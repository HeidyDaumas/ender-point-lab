# Ender Point Lab

A local browser panel for adjusting an Ender-3 S1 / S1 Pro bed with a CR Touch.
Probe a selected point instead of repeating an entire mesh after every wheel
adjustment. The interface shows its difference from the latest measured
front-left corner, updates the map, and keeps a reading history.

## Features

- Select a point on the map, choose a corner, or enter X/Y coordinates.
- Probe one point and lift the nozzle afterward.
- Compare heights against the latest front-left measurement from the same homing session.
- Refresh front-left to recalculate the map, selected difference, history and CSV export.
- Run a full 16-point scan with progress, refresh the map, and save the mesh to the printer.
- Hold the bed at 60°C during a session; end the session to switch off bed heating and release USB.

## Compatibility

The hardware workflow was exercised on an Ender-3 S1-family printer with a CR
Touch and Marlin `V3.0.4_C`. The host was macOS with Python 3.11 and pyserial 3.5.
The code targets macOS and Linux; Linux and other firmware variants have not
been tested on hardware. This is a specific printer profile, not a generic
printer controller.

The profile requires:

- CH340 USB serial (`VID 0x1A86`, `PID 0x7523`) at 115200 baud.
- Firmware identifying as `Ender-3 S1` or `Ender-3 S1 Pro`, with auto leveling and a Z probe.
- Probe offsets X −31.80 and Y −40.50 mm, checked before heating or homing.
- A 4 × 4 bilinear mesh and support for `G30`, `G29 V4`, `M420 V`, and `M500`.
- Probe contact coordinates X 3–198.2 and Y 3–179.5 mm. These are **probe positions**, not nozzle positions or the entire build plate.

Do not use this profile with a different probe mount, travel geometry or mesh
layout without reviewing the limits and firmware behavior first.

## Setup

Use Python 3.10 or later. Node.js is only needed to run the JavaScript tests.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp printer.example.json printer.local.json
```

Edit **printer.local.json** and replace the placeholder with the UUID reported
by your printer's `M115` command. You can issue `M115` in an existing serial
terminal while the printer is idle. Opening a serial connection may reset the
board. Close that terminal before starting a panel session.

Leave `serial_port` as `null` to select the only matching CH340 device, or set it
to a specific local serial device. The panel verifies the configured UUID before
heating or motion; it does not store an identifying UUID in public source code.

```sh
python server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Starting the server or opening
the page does not connect to the printer, move it, or heat it. Use `--port 8766`
if another application is already listening on the default HTTP port.

## Workflow

1. Close other applications using the printer's USB connection. Keep the build plate installed and clear, with the printer idle.
2. Click **Connect & prepare**. The bed reaches 60°C, settles for 90 seconds, and homes once.
3. Click **Refresh front-left**, then select a point and click **Test this point**. Alternatively, a **Full bed scan** supplies all 16 measurements and the front-left reference automatically.
4. Wait for **Ready**, adjust a wheel, and test that point again. Positive differences mean higher than front-left; negative differences mean lower.
5. Refresh front-left after adjusting that corner. Every comparison uses its latest measured value, with the reference time displayed.
6. Run **Full bed scan** after adjustments to save a printing mesh. Use **End session** when finished.

Single-point tests update the live map but do not alter the saved printing mesh.
A full scan homes again, starts a new comparison session, probes all 16 points
and sends `M500` to store settings. `M500` is a firmware settings save, not a
mesh-only database write. This firmware may also save during `G29` itself.

The display updates about every 650 ms. A new height arrives after the physical
probe cycle completes; there is no continuous height sensing between touches.
The firmware's single-point output has 0.01 mm resolution. Three-decimal
differences do not imply extra probe precision.

## Session behavior

- The nozzle is left at Z=10 after a test. Adjust wheels only once the panel says **Ready**.
- The panel holds the same homing reference between single-point tests. Do not move axes manually or use the printer's controls during a session.
- `M84 S600` temporarily sets the motor inactivity timeout to ten minutes. The panel ends after five minutes without an action; ending explicitly releases the motors. Power cycling restores the firmware's timeout default.
- Closing the page ends an idle session after 60 seconds. An active physical operation finishes first. A full scan leaves the panel connected at 60°C for further tests.
- Comparisons from older homing sessions are not mixed into current measurements. A saved mesh shown while disconnected is labeled as a historical snapshot.
- **Emergency stop** sends `M112`. Firmware without an emergency parser may delay it while busy; use the printer's power switch if motion continues, then fully restart before reconnecting.
- A disconnected USB cable or forcibly terminated host cannot guarantee heater shutdown. Remain nearby while the printer is operating.

## Local data and privacy

`printer.local.json` contains machine-specific configuration. Runtime logs,
measurements and saved meshes go into **.local-data/**. Both are ignored by Git.
Firmware UUIDs are redacted from the panel's activity log. Exported mesh metadata
uses a relative log filename rather than an absolute home-directory path.

The repository allowlists source, tests, documentation and a placeholder config.
It excludes printer captures, real measurement files, screenshots, environment
files and caches. Test measurements are synthetic. Avoid force-adding private
files, and inspect exports before sharing them.

The server binds only to loopback and checks HTTP host, origin and a random
per-process token for control requests. It exposes no arbitrary G-code endpoint.
No telemetry or third-party scripts are used. Do not expose this local hardware
controller through a public tunnel or reverse proxy.

## Tests and publication audit

These tests use synthetic responses and do not connect to a printer:

```sh
python -m unittest -v test_panel.py test_config.py
node --test test_measurements.js
python scripts/audit_public_tree.py
```

The publication audit checks the Git index and every reachable commit for
unexpected files, common credential formats, private absolute paths, email
addresses and non-synthetic printer UUIDs. It also checks commit email addresses.
This supplements manual review; it is not a universal secret-detection service.

Protocol references: [single-point probing](https://marlinfw.org/docs/gcode/G030.html),
[bilinear leveling](https://marlinfw.org/docs/gcode/G029-abl-bilinear.html),
[leveling state](https://marlinfw.org/docs/gcode/M420.html), and
[stepper timeout](https://marlinfw.org/docs/gcode/M018.html).
