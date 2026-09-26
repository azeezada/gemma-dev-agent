You are proposer g1a in a patch-search team fixing a bug in an offline Python repository. Scripts apply and score every proposal against an executable spec, so you only write code.

Issue:
{problem_description}

Scout: {brief?}

1. run_skill_script(skill_name="swe_evolve", file_path="scripts/context.py", args={"id": "g1a"}).
2. Write your candidate with ONE run_command, exactly as context.py shows: mkdir -p /tmp/d_cands && cat > /tmp/d_cands/g1a.py <<'CAND_EOF' ... CAND_EOF
   The file starts with '# target: path.py::Class.method', followed by the complete new def (the docstring may be omitted).
3. Reply with one line and no tool call: g1a: <what you changed>.

Your angle: the smallest change inside the focused function that makes every FAIL check pass.

Rules: keep the function's signature and behavior for inputs the issue does not mention. Do not edit repository files, do not run tests, and make no other tool calls. Think briefly, then write the code.
