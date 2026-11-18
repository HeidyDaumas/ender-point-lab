const test = require('node:test');
const assert = require('node:assert/strict');
const P = require('./measurements.js');
const mesh = {session:4, measured_at:'2000-01-01T12:00:00Z',
  mesh_rows_as_reported:[[.1,.2,.3,.4],[0,0,0,0],[0,0,0,0],[-.2,-.1,0,.1]]};
const state = () => ({connected:true,session:4,session_mesh:mesh,mesh,readings:[]});
const reading = (x,y,z) => ({x,y,z,session:4,time:'2000-01-01T12:01:00Z'});

test('full scan immediately supplies the front-left and all 16 differences',()=>{
  const v=P.current(state());
  assert.equal(v.points.size,16);
  assert.equal(v.reference.z,.1);
  assert.equal(P.difference(v.points.get(P.key(198.2,3)),v.reference),.3);
  assert.equal(P.difference(v.reference,v.reference),0);
});
test('new point reading replaces its scan value on the map and readout',()=>{
  const s=state();s.readings.push(reading(198.2,3,.25));
  const v=P.current(s),m=P.mapView(s);
  assert.equal(v.points.get(P.key(198.2,3)).source,'point');
  assert.equal(P.difference(v.points.get(P.key(198.2,3)),v.reference),.15);
  assert.equal(P.difference(m.points.get(P.key(198.2,3)),m.reference),.15);
});
test('refreshing front-left rebases every existing point without retesting it',()=>{
  const s=state();s.readings.push(reading(198.2,3,.25),reading(3,3,.15));
  const v=P.current(s);
  assert.equal(v.reference.z,.15);
  assert.equal(P.difference(v.points.get(P.key(198.2,3)),v.reference),.1);
  assert.equal(P.difference(v.points.get(P.key(3,179.5)),v.reference),-.35);
  assert.equal(P.difference(v.points.get(P.key(3,3)),v.reference),0);
  assert.equal(P.difference(s.readings[0],v.reference),.1); // history/export use same calculation
});
test('new homing cannot reuse an older scan or reference',()=>{
  const s=state();s.session=5;s.readings.push(reading(3,3,.2));
  assert.equal(P.current(s).reference,null);
  assert.equal(P.current(s).points.size,0);
  assert.equal(P.mapView(s).mode,'live');
  assert.equal(P.mapView(s).points.size,0);
});
test('a point can be measured before front-left without inventing a difference',()=>{
  const s=state();s.session_mesh=null;s.readings=[reading(110,110,.2)];
  const v=P.current(s);
  assert.equal(v.points.size,1);
  assert.equal(v.reference,null);
  assert.equal(P.difference(v.points.get(P.key(110,110)),v.reference),null);
  s.readings.push(reading(3,3,.1),reading(3,3,.12));
  const latest=P.current(s);
  assert.equal(P.difference(latest.points.get(P.key(110,110)),latest.reference),.08);
});
test('saved snapshot stays separate from live comparisons while disconnected',()=>{
  const s=state();s.connected=false;
  assert.equal(P.current(s).reference,null);
  assert.equal(P.mapView(s).mode,'saved');
  assert.equal(P.mapView(s).points.size,16);
});
test('mesh coordinates match rounded coordinates returned by the printer',()=>{
  assert.deepEqual(P.grid[6],{x:133.13,y:61.83,row:1,column:2});
  assert.equal(P.key(3,3),P.key(3.00,3.00));
});
