You finish a patch search. Do exactly these three tool calls in order, then reply "done":
1. run_skill_script(skill_name="swe_evolve", file_path="scripts/trial.py", args={}) scores the last candidates.
2. run_skill_script(skill_name="swe_evolve", file_path="scripts/final.py", args={}) leaves the best patch in the workspace.
3. submit_patch, called even if the diff looks imperfect: the best patch found must be submitted.
Do not edit files or run anything else.
