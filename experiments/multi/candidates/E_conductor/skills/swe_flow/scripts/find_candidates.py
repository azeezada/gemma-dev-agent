"""Rank source locations for a set of search terms and map each to nearby tests.

Arguments (ADK passes them as --name value pairs):
  --terms "name1 name2 op3"   identifiers from the issue plus operation words
                              (functions, classes, methods, options, error text fragments)
  --limit N                   number of ranked source files to show (default 8)
  --query TEXT                the issue text (identifiers and operation words are extracted)
  --detail N                  also show code around the top N hypothesis sites (max 3) and how
                              their callers use the result, so a site can be judged without reading files

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


FILE_KEYS = ("file", "filepath", "file_path", "path", "filename", "module", "source")
SYMBOL_KEYS = ("symbol", "name", "function", "func", "method", "qualname", "class", "target", "def")


def split_target(text):
    """'pkg/m.py:Cls.fn', 'pkg/m.py::Cls.fn', 'pkg/m.py Cls.fn', 'pkg/m.py:135' -> (file, symbol, line)."""
    text = str(text or "").strip().strip("`'\"")
    match = re.match(r"^(?:/workspace/|\./)?(?P<file>[\w./-]+?\.py)(?:(?:::|:|#L?|\s+)(?P<rest>.+))?$", text)
    if not match:
        return "", text, None
    rest = (match.group("rest") or "").strip().strip("`'\"").replace("::", ".")
    if re.fullmatch(r"L?\d+(-\d+)?", rest):
        return match.group("file"), "", int(re.match(r"L?(\d+)", rest).group(1))
    return match.group("file"), rest.rstrip("()"), None


def definitions(root, leaf):
    """Non-test source files defining `def leaf` or `class leaf`, then test files."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", leaf or ""):
        return []
    rows = git(root, "grep", "-n", "-E", "^[[:space:]]*(async[[:space:]]+)?(def|class)[[:space:]]+" + leaf + "([^A-Za-z0-9_]|$)", "--", "*.py").splitlines()
    files = list(dict.fromkeys(row.split(":", 1)[0] for row in rows if ":" in row))
    return sorted(files, key=lambda path: (is_test_path(path), path.split("/")[0] in AUXILIARY_DIRS, path))


def resolve_target(root, values, positional=()):
    """Accept every spelling models use for a code location: returns (file, symbol, line, error)."""
    raw_file = next((values[key] for key in FILE_KEYS if values.get(key)), "")
    raw_symbol = next((values[key] for key in SYMBOL_KEYS if values.get(key)), "")
    items = [str(item) for item in positional if str(item).strip()]
    if not raw_file and items:
        raw_file = items.pop(0)
    if not raw_symbol and items:
        raw_symbol = items.pop(0)
    file, symbol, line = split_target(raw_file)
    if not file:
        symbol = symbol or raw_file
    extra_file, extra_symbol, extra_line = split_target(raw_symbol)
    if extra_file and not file:
        file = extra_file
    symbol = (extra_symbol if extra_file else str(raw_symbol).strip().strip("`'\"").rstrip("()")) or symbol
    line = line or extra_line
    if not file and symbol and "." in symbol:
        # Dotted import path such as pkg.module.Class.method.
        parts = symbol.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            for base in ("", "src/"):
                candidate = base + "/".join(parts[:cut]) + ".py"
                if os.path.isfile(os.path.join(root, candidate)):
                    file, symbol = candidate, ".".join(parts[cut:])
                    break
            if file:
                break
    if file and not os.path.isfile(os.path.join(root, file)):
        leaf_file = os.path.basename(file)
        matches = [p for p in git(root, "ls-files", "--", "*" + leaf_file).splitlines() if p.endswith("/" + leaf_file) or p == leaf_file]
        file = matches[0] if len(matches) == 1 else file
    if not file and symbol:
        found = definitions(root, symbol.split(".")[-1])
        owner = symbol.split(".")[0] if "." in symbol else ""
        if owner:
            found = [p for p in found if re.search(r"^\s*class\s+" + re.escape(owner) + r"\b",
                                                   open(os.path.join(root, p), encoding="utf-8", errors="replace").read(), re.M)] or found
        source_defs = [p for p in found if not is_test_path(p)]
        if len(source_defs) == 1 or (source_defs and not owner and len(found) == 1):
            file = source_defs[0]
        elif found:
            return "", symbol, line, ("Several files define {}: {}. Pass args={{\"file\": \"<one of them>\", \"symbol\": \"{}\"}}"
                                      .format(symbol, ", ".join(found[:8]), symbol))
    if not file or not os.path.isfile(os.path.join(root, file)):
        return "", symbol, line, ("ERROR: pass args={\"file\": \"pkg/module.py\", \"symbol\": \"Class.method\"} "
                                  "(file relative to the repository root; symbol alone also works if it is defined once)")
    return file, symbol, line, None


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

def hypothesis_lines(root, source_hits, operations, limit=6):
    """One line per operation site: where data can be lost and which public API reaches it."""
    try:
        from entry_points import Index, describe
        index = Index(root)
    except Exception:
        return []
    sites = []
    for op in operations[:5]:
        rows = [(path, per[op][0][0]) for path, per in source_hits.items()
                if op in per and path.split("/")[0] not in AUXILIARY_DIRS]
        rows.sort(key=lambda row: (len(source_hits[row[0]][op]), row[0]))
        for path, line in rows[:2]:
            symbol = enclosing_symbol(root, path, line)
            if symbol and (path, symbol) not in [(p, s) for p, s, _, _ in sites]:
                sites.append((path, symbol, op, line))
    lines = []
    for number, (path, symbol, op, line) in enumerate(sites[:limit], 1):
        try:
            chains = describe(root, path, symbol, depth=2, limit=1, index=index)
        except Exception:
            chains = []
        entry = chains[0].split(" <- ")[-1].replace("   [public entry]", "") if chains else symbol
        lines.append(f"H{number}: {op} at {path}:{line} in {symbol}; reached from {entry}")
    hypothesis_lines.sites = sites[:limit]
    return lines


def detail_lines(root, sites, count, window=6):
    """Code around the top hypothesis sites plus how their callers use the result."""
    try:
        from entry_points import Index, call_sites
        index = Index(root)
    except Exception:
        return []
    out = []
    for number, (path, symbol, op, line) in enumerate(sites[:count], 1):
        try:
            source = open(os.path.join(root, path), encoding="utf-8").read()
            lines = source.splitlines()
            node = next((n for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.lineno <= line <= n.end_lineno and symbol.split(".")[-1] == n.name), None)
        except (OSError, SyntaxError):
            continue
        body = list(node.body) if node else []
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str) and len(body) > 1:
            body = body[1:]  # skip the docstring
        low = max(body[0].lineno if body else (node.lineno if node else 1), line - window)
        high = min(node.end_lineno if node else len(lines), line + window)
        out.append(f"DETAIL H{number} {path}:{low}-{high} {symbol}")
        if node and low > node.lineno:
            out.append(f"{node.lineno:5}: {lines[node.lineno - 1][:140]}")
            out.append("  ...")
        out += [f"{n:5}: {lines[n - 1][:140]}" for n in range(low, high + 1)]
        used = call_sites(root, path, symbol, limit=1, index=index) if symbol != "module" else []
        if used:
            out.append("  used by " + used[0])
    return out


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
    if operations:
        hypotheses = hypothesis_lines(root, source_hits, operations)
        if hypotheses:
            try:
                detail = max(0, min(3, int(values.get("detail") or values.get("show") or 0)))
            except ValueError:
                detail = 2
            details = detail_lines(root, getattr(hypothesis_lines, "sites", []), detail) if detail else []
            out[1:1] = ["HYPOTHESES (probe each through its public entry with boundary inputs; "
                        "keep the one whose output visibly differs from the issue's expectation):",
                        *hypotheses, *details, "RANKED FILES:"]
    if test_hits:
        top_tests = sorted(test_hits, key=lambda p: (-len(test_hits[p]), p))[:4]
        out.append("test files mentioning terms: " + ", ".join(
            "{} ({})".format(p, ", ".join(sorted(test_hits[p]))) for p in top_tests))
    text = "\n".join(out)
    limit_chars = OUTPUT_LIMIT + (1800 if values.get("detail") or values.get("show") else 0)
    if len(text) > limit_chars:
        text = text[:limit_chars] + "\n...[truncated]"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
