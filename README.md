# SolidForms Auto-Eject (online)

Batch and auto-ejection G-code for the **Elegoo Centauri Carbon** and the **Bambu Lab P1S**, in the browser.

Drop one sliced plate (`.gcode`, or `.gcode.3mf` for the P1S), set the number of units and the cooldown, and download one file that prints it N times, pushing each finished part off the bed before starting the next. The file is re-read and verified before you print it.

**Your files never leave your computer.** The engine runs inside the browser (Python via [Pyodide](https://pyodide.org)); nothing is uploaded.

This is the same engine as the SolidForms Auto-Eject desktop app. The site is built from the app's source with `web/build_web.py`; don't edit these files by hand.

Before every run: front door open or removed, a bin in front of the printer, last batch's purge lanes scraped off, and watch the first ejection of every new product.
