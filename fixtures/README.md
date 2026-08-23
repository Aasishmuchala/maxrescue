# fixtures

Drop a real `.max` here to validate the X-ray against something other than
synthetic containers. Ideally two files:

- the monster scene (170–192 GB open), and
- any small ordinary scene, as a control.

Nothing here is committed — see `.gitignore`.

Then:

```bash
../.venv/bin/python -m maxrescue.app.cli xray scene.max
```
