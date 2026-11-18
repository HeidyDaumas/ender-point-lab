import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import server

MESH = '''Bilinear Leveling Grid:
  0      1      2      3
0 +0.100 +0.200 +0.300 +0.400
1 +0.050 +0.100 +0.150 +0.200
2 -0.100 -0.050 +0.000 +0.050
3 -0.200 -0.150 -0.100 -0.050
Subdivided with CATMULL ROM Leveling Grid:
0 +0.100 +0.120 +0.140 +0.160
echo:Bed Leveling ON
ok'''


class ProbeTests(unittest.TestCase):
    def test_invalid_coordinates_cannot_become_commands(self):
        for pair in ((0,100),(200,100),(100,180),(float('nan'),3),(float('inf'),3),(True,3),('3\nG28',3)):
            with self.subTest(pair=pair), self.assertRaises((TypeError,ValueError)):
                server.point(*pair)
        for cmd in ('G1 X300', 'M104 S200', 'G29', 'G29 W I0 J0 Z4', 'G30 X3 Y3\nG28', 'G30 X999 Y3'):
            self.assertFalse(server.allowed(cmd),cmd)
        self.assertTrue(server.allowed('G30 X198.20 Y179.50'))

    def test_probe_result_must_match_requested_position(self):
        self.assertEqual(server.probe_result('Bed X: 198.20 Y: 3.00 Z: -0.15\nok',198.2,3),-.15)
        for response in ('ok','Error: Probing Failed','Bed X: 3.00 Y: 3.00 Z: 0.1','Bed X: 198.20 Y: 3.00 Z: 8.0'):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                server.probe_result(response,198.2,3)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        folder=Path(self.tmp.name)/'panel'
        folder.mkdir()
        self.root=patch.object(server,'ROOT',folder)
        self.root.start()
        self.c=server.Controller()

    def tearDown(self):
        self.c.logfile.close()
        self.root.stop()
        self.tmp.cleanup()

    def test_one_point_and_delta_without_rehoming_or_mesh_write(self):
        commands=[]
        values=iter([.30,.20])
        def fake(code,timeout=20):
            commands.append(code)
            if code=='M27':return 'Not SD printing\nok'
            if code=='M105':return 'ok T:25 /0 B:60 /60'
            if code.startswith('G30'):return f'Bed X: 198.20 Y: 3.00 Z: {next(values)}\nok'
            return 'ok'
        self.c.command=fake
        self.c.owns_heat=True
        self.c.update(ready=True,session=1)
        self.c.probe({'x':198.2,'y':3})
        self.c.probe({'x':198.2,'y':3})
        self.assertEqual(sum(x.startswith('G30') for x in commands),2)
        self.assertFalse(any(x in ('G28','G29','M500') for x in commands))
        self.assertEqual(commands[-2:],['G1 Z10 F300','M400'])
        self.assertEqual(self.c.state['readings'][-1]['delta'],-.1)

    def test_no_probe_without_session_and_no_duplicate_jobs(self):
        with self.assertRaises(ValueError):self.c.submit('probe',{'x':3,'y':3})
        self.c.update(ready=True)
        self.c.submit('probe',{'x':3,'y':3})
        with self.assertRaises(ValueError):self.c.submit('probe',{'x':3,'y':3})

    def test_reference_cannot_be_switched_to_an_arbitrary_point(self):
        ref={'x':3,'y':3,'z':.1,'session':2}
        self.c.update(ready=True,session=2,front_left=ref,readings=[{'id':1,'session':2,'x':198.2,'y':3}])
        with self.assertRaises(ValueError):self.c.submit('reference',{'id':1})
        self.assertEqual(self.c.state['front_left'],ref)

    def test_retesting_front_left_updates_reference_automatically(self):
        values=iter([.20,.05])
        def fake(code,timeout=20):
            if code=='M27':return 'Not SD printing\nok'
            if code=='M105':return 'ok T:25 /0 B:60 /60'
            if code.startswith('G30'):return f'Bed X: 3.00 Y: 3.00 Z: {next(values)}\nok'
            return 'ok'
        self.c.command=fake
        self.c.owns_heat=True
        self.c.update(ready=True,session=1)
        self.c.probe({'x':3,'y':3})
        self.assertEqual(self.c.state['front_left']['z'],.2)
        self.c.probe({'x':3,'y':3})
        self.assertEqual(self.c.state['front_left']['z'],.05)
        self.assertEqual(self.c.state['front_left']['session'],1)
        self.assertEqual(self.c.state['front_left']['source'],'point')

    def test_ending_session_invalidates_front_left_and_live_mesh(self):
        self.c.update(front_left={'z':.1},session_mesh={'session':1})
        self.c.end()
        self.assertIsNone(self.c.state['front_left'])
        self.assertIsNone(self.c.state['session_mesh'])

    def fake_full_scan(self, mesh=MESH):
        commands=[]
        def fake(code,timeout=20,on_line=None):
            commands.append(code)
            if code=='M27':return 'Not SD printing\nok'
            if code=='M105':return 'ok T:25 /0 B:60 /60'
            if code=='M420 V':return mesh
            if code=='G29 V4':
                self.assertEqual(timeout,240)
                for n in range(1,17):
                    on_line(f'Probing mesh point {n}/16.')
                    self.assertEqual(self.c.state['scan_progress'],n)
            return 'ok'
        self.c.command=fake
        self.c.owns_heat=True
        self.c.update(connected=True,ready=True,session=1,front_left={'id':1})
        return commands

    def test_full_scan_saves_complete_mesh_and_refreshes_front_left(self):
        commands=self.fake_full_scan()
        self.c.full_scan()
        self.assertLess(commands.index('G28'),commands.index('G29 V4'))
        self.assertLess(commands.index('M420 V'),commands.index('M500'))
        self.assertIn('G1 Z10 F300',commands[commands.index('M500'):])
        self.assertEqual(self.c.state['mesh']['range_mm'],.6)
        self.assertEqual(len(self.c.state['mesh']['mesh_rows_as_reported']),4)
        self.assertTrue(self.c.state['ready'])
        self.assertEqual(self.c.state['front_left']['z'],.1)
        self.assertEqual(self.c.state['front_left']['session'],2)
        self.assertEqual(self.c.state['session_mesh']['session'],2)
        self.assertIsNone(self.c.state['scan_progress'])
        self.assertEqual(self.c.state['session'],2)
        self.assertTrue(self.c.restore_leveling)
        files=list(self.c.data_dir.glob('fresh-mesh-*.json'))
        self.assertEqual(len(files),1)
        self.assertTrue(json.loads(files[0].read_text())['saved_to_eeprom'])

    def test_bad_mesh_never_replaces_map_or_requests_settings_save(self):
        for bad in (MESH.replace('3 -0.200 -0.150 -0.100 -0.050',''),
                    MESH.replace('+0.300','nan'),MESH.replace('+0.300','+9.000'),
                    MESH.replace('Leveling ON','Leveling OFF')):
            commands=self.fake_full_scan(bad)
            old={'range_mm':.352}
            self.c.update(mesh=old)
            with self.assertRaises(RuntimeError):self.c.full_scan()
            self.assertNotIn('M500',commands)
            self.assertEqual(self.c.state['mesh'],old)
        self.assertEqual(list(self.c.data_dir.glob('fresh-mesh-*.json')),[])

    def test_disconnected_full_scan_prepares_without_second_homing(self):
        commands=self.fake_full_scan()
        self.c.update(connected=False,ready=False)
        def prepare():
            commands.append('G28')
            self.c.update(connected=True,ready=True,session=2)
        self.c.prepare=prepare
        self.c.full_scan()
        self.assertEqual(commands.count('G28'),1)
        self.assertEqual(commands.count('G29 V4'),1)

    def test_full_scan_can_be_requested_disconnected_but_not_queued_twice(self):
        self.c.submit('full_scan',{})
        self.assertEqual(self.c.jobs.get_nowait()[0],'full_scan')
        with self.assertRaises(ValueError):self.c.submit('full_scan',{})

    def test_serial_lines_are_forwarded_for_live_progress(self):
        class FakeSerial:
            def __init__(self):self.lines=iter([b'Probing mesh point 1/16.\n',b'ok\n'])
            def write(self,data):pass
            def flush(self):pass
            def readline(self):return next(self.lines)
        self.c.ser=FakeSerial()
        received=[]
        self.c.command('G29 V4',240,on_line=received.append)
        self.assertEqual(received,['Probing mesh point 1/16.','ok'])


if __name__=='__main__':unittest.main()
