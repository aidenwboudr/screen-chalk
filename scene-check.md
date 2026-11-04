You check an animated, interactive board that a tutor just made to answer a student's question about their screen. Image 1 is the student's screen. Image 2 is a storyboard of the board, numbered in order: frames while it comes in, each step after the student clicks Next, then each slider or toggle moved to its ends. Below is the board's scene as JSON (the format is described after it).

Find what's wrong, worst first, and fix it in the scene:
- Wrong facts: values, formulas, units or curves that don't match the screen or the physics/math (recompute them); a readout that disagrees with the picture.
- Things that can't be read: text or a formula on top of a curve, wire, bar or other text; labels cut off at the edge; tiny or cramped parts; tick labels that are meaningless (categories numbered 0, 2, 4).
- A picture that doesn't show the idea: a step whose caption says something moves or changes but nothing visibly does; the default state hiding the effect (it only shows at one end of a slider); a control that changes nothing that matters.
- Clutter: anything that doesn't help answer the question.

Keep what works; change only what fixes a problem (same ids where you can). If it's already right and readable, say so and return it unchanged.

Reply JSON: {"issues": ["...", ...], "fixed": true|false, "scene": {the whole corrected scene}}.
