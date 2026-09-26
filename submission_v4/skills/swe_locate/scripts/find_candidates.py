"""Rank source locations for a set of search terms and map each to nearby tests.

Arguments (ADK passes them as --name value pairs):
  --terms "name1 name2 op3"   identifiers from the issue plus operation words
                              (functions, classes, methods, options, error text fragments)
  --limit N                   number of ranked source files to show (default 8)

Output: a ranked ledger of source files with the matching lines, the test files
that exercise each file or its terms, and the last commit touching it.
"""
import ast
import os
import re
import subprocess
import sys
from collections import defaultdict

OUTPUT_LIMIT = 4800
MAX_HITS_PER_TERM = 20000
AUXILIARY_DIRS = {"docs", "doc", "docs_src", "examples", "example", "scripts", "benchmarks",
                  "bench", "tools", "tutorial", "tutorials"}


def parse_args(argv):
    values, positional, index = {}, [], 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positional.extend(argv[index + 1:])
            break
        if token.startswith("--") and "=" in token:
            key, value = token[2:].split("=", 1)
            values[key.replace("-", "_")] = value
            index += 1
        elif token.startswith("--"):
            value = argv[index + 1] if index + 1 < len(argv) else ""
            values[token[2:].replace("-", "_")] = value
            index += 2
        else:
            positional.append(token)
            index += 1
    return values, positional


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return [str(item) for item in parsed]
        except (ValueError, SyntaxError):
            pass
    return [item for item in re.split(r"[\s,]+", text) if item]


def workspace_root():
    for candidate in (os.environ.get("SWE_WORKSPACE"), "/workspace"):
        if candidate and os.path.isdir(os.path.join(candidate, ".git")):
            return os.path.realpath(candidate)
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if top:
            return top
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getcwd()


def git(root, *args):
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_test_path(path):
    parts = path.split("/")
    name = parts[-1]
    return (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
            or any(part in {"tests", "test", "testing"} for part in parts[:-1]))


def clean_term(term):
    term = term.strip().strip("`'\"()[]{}:,;")
    if term.endswith("()"):
        term = term[:-2]
    return term


def grep(root, term):
    word = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", term) is not None
    args = ["grep", "-n", "-I", "-F"]
    if word:
        args.append("-w")
    args += ["-e", term, "--", "*.py"]
    hits = []
    for line in git(root, *args).splitlines()[:MAX_HITS_PER_TERM]:
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            hits.append((parts[0], int(parts[1]), parts[2]))
    return hits


def related_tests(root, path, terms, test_hits, limit=3):
    """Tests named after the module, then tests importing it, then tests sharing its terms."""
    module = os.path.splitext(os.path.basename(path))[0]
    if module == "__init__":
        module = os.path.basename(os.path.dirname(path))
    dotted = os.path.splitext(path)[0].replace("/", ".")
    if dotted.startswith("src."):
        dotted = dotted[4:]
    if dotted.endswith(".__init__"):
        dotted = dotted[: -len(".__init__")]
    tests = [p for p in git(root, "ls-files", "--", "*.py").splitlines() if is_test_path(p)]
    named = [p for p in tests if os.path.basename(p) in ("test_{}.py".format(module), "{}_test.py".format(module))]
    importing = [p for p in git(root, "grep", "-l", "-F", "-e", dotted, "--", *tests).splitlines()] if tests and "." in dotted else []
    sharing = sorted((p for p, ts in test_hits.items() if ts & terms),
                     key=lambda p: (-len(test_hits[p] & terms), p))
    ordered = []
    for group in (named, importing, sharing):
        for p in group:
            if p not in ordered:
                ordered.append(p)
    return ordered[:limit]


# General operation families, never repository names, task ids or solution snippets.
OPERATION_FAMILIES = {
    "newline whitespace blank trailing": ["splitlines", "rstrip", "strip", "split", "join"],
    "decode deserialize serialization pickle": ["decode", "loads", "__reduce__", "__getstate__"],
    "pipe output flush write": ["write", "flush", "close", "__exit__"],
    "connection server stream lifecycle shutdown": ["close", "reset", "finally", "__exit__"],
    "circular recursion recursive include": ["append", "extend"],
    "credentials authentication password login": ["auth", "authenticators", "netrc"],
}
STOPWORDS = set("the and for with this that from when not are fix add use none true false self return should issue error fixes support allow make into have has was were which will see full bug report a an to of in on is it as by be or at its".split())


def query_terms(query):
    query = re.sub(r"https?://\S+", " ", query)
    explicit = re.findall(r"`([^`\n]+)`", query)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", " ".join(explicit) + " " + query)
    terms = []
    for token in tokens:
        if token.lower() not in STOPWORDS and token not in terms:
            terms.append(token)
    # Morphology: a plural title must find singular docstrings.
    for token in list(terms):
        if token.endswith("s") and len(token) > 4:
            terms.append(token[:-1])
    operations = []
    words = set(token.lower() for token in terms)
    for family, values in OPERATION_FAMILIES.items():
        if words.intersection(family.split()):
            operations.extend(values)
    return list(dict.fromkeys(terms))[:24], list(dict.fromkeys(operations))


def enclosing_symbol(root, path, line):
    try:
        tree = ast.parse(open(os.path.join(root, path), encoding="utf-8").read())
        def walk(node, prefix=""):
            for child in getattr(node, "body", []):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + child.name
                    if child.lineno <= line <= child.end_lineno:
                        yield name, child
                        yield from walk(child, name + ".")
        entries = list(walk(tree))
        return entries[-1][0] if entries else "module"
    except (OSError, SyntaxError):
        return "module"

def main(argv):
    values, positional = parse_args(argv)
    query, operations = query_terms(values.get("query", ""))
    terms = []
    for term in as_list(values.get("terms")) + as_list(values.get("term")) + positional + query + operations:
        term = clean_term(term)
        if len(term) >= 3 and term not in terms:
            terms.append(term)
    try:
        limit = max(1, min(20, int(values.get("limit", 8))))
    except ValueError:
        limit = 8
    if not terms:
        print("ERROR: pass --terms with identifiers from the issue and operation words")
        return 2
    root = workspace_root()
    source_hits = defaultdict(lambda: defaultdict(list))
    test_hits = defaultdict(set)
    term_counts = {}
    for term in terms:
        hits = grep(root, term)
        term_counts[term] = len(hits)
        for path, line_no, text in hits:
            if is_test_path(path):
                test_hits[path].add(term)
            else:
                source_hits[path][term].append((line_no, text.strip()))
    if not source_hits:
        counts = ", ".join("{}={}".format(t, c) for t, c in term_counts.items())
        print("No source matches for any term ({}). Try other identifiers or operation words."
              .format(counts))
        return 1

    # Rare terms are more informative than common ones.
    files_per_term = defaultdict(int)
    for path, per_term in source_hits.items():
        for term in per_term:
            files_per_term[term] += 1

    def defines(term, lines):
        pattern = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+{0}\b|^{0}\s*[:=]".format(re.escape(term)))
        return any(pattern.search(text) for _, text in lines)

    def score(path):
        per_term = source_hits[path]
        rarity = sum(1.0 / files_per_term[term] for term in per_term)
        density = sum(min(len(lines), 5) for lines in per_term.values()) * 0.05
        definitions = sum(8 for term, lines in per_term.items() if defines(term, lines))
        value = sum((0.5 if term in operations else 3) / (files_per_term[term] ** 0.5) for term in per_term) + rarity + density + definitions
        # Documentation examples and helper scripts can be the fix location, but they
        # usually mirror the library, so rank them below package code with equal evidence.
        if path.split("/")[0] in AUXILIARY_DIRS:
            value *= 0.5
        return value

    ranked = sorted(source_hits, key=lambda path: (-score(path), path))[:limit]
    # Reserve a few slots for distinct destructive operations on underspecified
    # issues. This avoids eight variants of the same high-vocabulary module.
    if operations and len(query) <= 6:
        primary = ranked[:max(1, limit - 3)]
        for op in operations:
            alternatives = [path for path, per in source_hits.items() if op in per and path not in primary
                            and path.split("/")[0] not in AUXILIARY_DIRS]
            alternatives.sort(key=lambda path: (len(source_hits[path][op]), path))
            if alternatives and len(primary) < limit:
                primary.append(alternatives[0])
        ranked = (primary + [path for path in ranked if path not in primary])[:limit]
    out = ["term hits: " + ", ".join("{}={}".format(t, c) for t, c in term_counts.items() if c)]
    for rank, path in enumerate(ranked, 1):
        per_term = source_hits[path]
        out.append("{}. {}  terms: {}".format(rank, path, ", ".join(sorted(per_term))))
        shown = sorted({lines[0] for lines in per_term.values()}, key=lambda row: (not any(op in row[1] for op in operations), row[0]))[:2]
        for line_no, text in shown:
            out.append("     L{} {}: {}".format(line_no, enclosing_symbol(root, path, line_no), text[:100]))
        related = related_tests(root, path, set(per_term), test_hits, limit=2)
        if related:
            out.append("     tests: " + ", ".join(related))

    # Surface diverse mechanisms even if a broad file matches many ordinary words.
    if operations:
        out.append("OPERATION MAP (alternative mechanisms, not proven causes):")
        for op in operations[:5]:
            matches = [(path, per[op][0]) for path, per in source_hits.items() if op in per]
            matches.sort(key=lambda row: (row[0].split("/")[0] in AUXILIARY_DIRS, len(source_hits[row[0]][op]), row[0]))
            out.append(op + ": " + "; ".join(f"{path}:{hit[0]} {enclosing_symbol(root, path, hit[0])}" for path, hit in matches[:3]))
    if test_hits:
        top_tests = sorted(test_hits, key=lambda p: (-len(test_hits[p]), p))[:4]
        out.append("test files mentioning terms: " + ", ".join(
            "{} ({})".format(p, ", ".join(sorted(test_hits[p]))) for p in top_tests))
    text = "\n".join(out)
    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT] + "\n...[truncated]"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
