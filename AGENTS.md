# Project access boundaries

- The project root is the directory containing the root `__init__.py` file.
- Files and directories anywhere inside this project root may be listed, searched, read, created, edited, and otherwise accessed without requesting per-file permission or requiring the user to provide each absolute path.
- Do not access files outside this project root unless the user gives explicit permission and provides the exact absolute path.
- Do not run, inspect, enumerate, search, or scan workflows.
