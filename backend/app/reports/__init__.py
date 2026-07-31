"""Result-directory analysis: pull a run's logs, chart them, save the review.

`runlog` reads one of the server's own result directories as the shape
`guides/server_results_guide.md` documents, and `runcharts` draws it as the
Part II catalogue of `guides/visual_guide.md`. `parse` is the fallback for a
directory nobody has seen before, `charts` picks between the two and owns the
generic forms, `style` is the one copy of the guide's §4 style block, `palette`
holds the guide's colors and the validator it demands, and `store` is the
on-disk report each analysis becomes.
"""

from __future__ import annotations

from . import charts, palette, parse, runcharts, runlog, store, style

__all__ = ["charts", "palette", "parse", "runcharts", "runlog", "store", "style"]
