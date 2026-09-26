---
name: swe_evolve
description: Spec-first patch search. Record the function to fix, check that the executable spec fails on the original code, show proposers everything they need, score all candidate patches by execution, and prepare the best one for submission.
---

Use run_skill_script with skill_name="swe_evolve". Arguments are flat, and none of them contain code.
Code (the spec and the candidates) is written to /tmp with run_command heredocs.

- scripts/focus.py: args={"target": "pkg/module.py::Class.method"}. Shows the function with line numbers and the lines that call it, and records it as the focus.
- scripts/spec.py: args={}. Runs /tmp/d_spec.py on the ORIGINAL code. It must FAIL there (DISCRIMINATING), or it proves nothing.
- scripts/context.py: args={"id": "g1a"}. Shows the spec, its failures, the focused code, the last trial report and the best candidate, plus the exact command for writing /tmp/d_cands/<id>.py.
- scripts/trial.py: args={}. Applies every new candidate to the original code, runs the spec and the related existing tests, ranks the candidates, and leaves the best one applied.
- scripts/final.py: args={}. Workspace = original code + best candidate, with no scratch files. Then call submit_patch.

Candidate file format (/tmp/d_cands/<id>.py):
```
# target: pkg/module.py::Class.method
def method(self, ...):
    ...complete new definition (the docstring may be omitted; the original is kept)...
```
Repeat the `# target:` header for each additional function. For code outside functions, use
`# replace: pkg/module.py` followed by `<<<<<<< OLD` / exact old lines / `=======` / new lines / `>>>>>>> NEW`.
