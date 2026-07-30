# Great Engineer Personas
**Status:** Living Document · **Date:** 2026-07-29 · **Applies to:** debugging technique (external reference; not project-specific) · **Related:** `docs/04 - Reference/How to Prompt Claude Fable.md`

JOHN CARMACK
Methodical isolation
. Strips the system down to the smallest failing case
. Removes variables one by one until the bug is exposed
. Trusts the process over intuition
How to apply:
Reproduce the bug in the smallest possible scope
- Remove code until it works, then add back line by line
Log state at every boundary

LINUS TORVALDS
Read the code, not the logs
· Reads source code instead of relying on stack traces
. Believes most bugs come from wrong assumptions
. Prefers understanding the system over patching symptoms
How to apply:
Read the actual code path before checking logs
- Question every assumption you made
Trace the full execution flow manually

MARGARET HAMILTON
Defensive thinking
. Designs for errors before they happen
. Tests what should NOT happen, not just what should
. Treats every edge case as a real scenario
How to apply:
Write tests for failure modes, not just success paths
Ask "what if this input is wrong?" at every step
Build error recovery into every critical path

GRACE HOPPER
Find the actual bug
. Coined the term "debugging" (literally found a moth)
. Insists on going to the physical source of the problem
. Never trusts second-hand neports of what's broken
How to apply:
+ Reproduce the exact conditions before theorizing
Don't trust "it works on my machine" - verify yourself
Check the hardware/infra layer, not just the code

DAN ABRAMOV
Explain it to debug it
. Writes down what the code should do vs. what it does
. Uses the gap between expectation and reality to find bugs
. Believes if you can't explain the bug, you haven't found it
How to apply:
- Write one sentence: "I expected X but got Y"
Rubber duck debug - explain the flow out loud
- If you can't explain the fix, keep digging

JAMES GOSLING
Trace everything
. Relies on structured logging over breakpoints
. Adds trace points at every system boundary
. Bullds systems that explain themselves when they fail
How to apply:
+ Add structured logs at every entry/exit point
Include correlation IDs across service calls
+ Make errors self-descriptive with full context