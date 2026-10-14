# README illustrations

These GIFs illustrate the documented workflow and the executed synthetic cancellation case.
They are authored diagrams, not terminal recordings. Motion timing does not represent run duration.
The case evidence remains in [examples/task-cancellation](../../examples/task-cancellation/README.md).

Each GIF opens on the complete overview or result. The corresponding PNG contains that same still.
Both animations loop slowly and retain their labels throughout each scene.

To regenerate, use Python with Pillow and the Lato Regular/Bold fonts in
`/usr/share/fonts/truetype/lato`, then run:

```bash
python3 assets/readme/render.py
```

The initial assets were generated with Pillow 12.3.0. Pillow and these fonts are authoring dependencies
only; Grounded Build's runtime dependencies are unchanged.
