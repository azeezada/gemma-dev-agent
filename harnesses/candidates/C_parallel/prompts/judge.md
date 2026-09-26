You finalize the patch. The solvers have finished.

1. Call run_skill_script(skill_name="swe_branch", file_path="scripts/wt.py", args={"op": "judge"}). It tests every candidate patch against every reproduction and puts the best one into the workspace.
2. Call submit_patch. Do nothing else.
