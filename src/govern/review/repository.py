"""Read-only Git capture. Mutable indexes/worktrees are content-addressed, never SHAs."""
import ast
import hashlib
import json
import os
import io
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess


SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".govern"}
SOURCE = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


def sensitive_path(name):
    path=PurePosixPath(name.lower());base=path.name
    return (base=='.env' or (base.startswith('.env.') and not base.endswith(('.example','.sample','.template')))
        or base in {'.npmrc','.netrc','id_rsa','id_ed25519','credentials','credentials.json','govern.config.json'}
        or bool(set(path.parts)&{'.ssh','.aws','.azure','.config'}) or base.endswith(('.pem','.key','.p12','.pfx')))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_path(root, relative):
    part = PurePosixPath(str(relative).replace("\\", "/"))
    if part.is_absolute() or ".." in part.parts or ":" in str(part):
        raise ValueError("unsafe repository path")
    result = Path(root).resolve().joinpath(*part.parts).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise ValueError("path escapes repository")
    return result


def git(root, *args, binary=False, check=True, stdin=None):
    p = subprocess.run(["git", "-c", "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
                        "-C", str(root), *args], capture_output=True, timeout=60,input=stdin,env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'})
    if check and p.returncode:
        raise ValueError(p.stderr.decode("utf-8", "replace")[:1200])
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def describe(path, skip=0, limit=40):
    root = git(path, "rev-parse", "--show-toplevel").strip()
    refs = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").splitlines()
    lines = git(root, "log", "--all", "--format=%H%x09%P%x09%s", f"--skip={max(0, int(skip))}", f"--max-count={min(100, max(1, int(limit)))}").splitlines()
    return {"root": root, "branch": git(root, "branch", "--show-current").strip(),
            "dirty": bool(git(root, "status", "--porcelain")), "refs": refs,
            "modified_files":git(root,"diff","--name-only","-z","HEAD").strip("\0").split("\0"),
            "untracked_files":git(root,"ls-files","--others","--exclude-standard","-z").strip("\0").split("\0"),
            "commits": [{"sha": a[0], "parents": a[1].split(), "subject": a[2]} for a in (line.split("\t", 2) for line in lines)]}


def _files(root, ref):
    if ref == "WORKTREE":
        names = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    elif ref == "INDEX":
        names = git(root, "ls-files", "-z", "--cached").split("\0")
    else:
        names = git(root, "ls-tree", "-r", "--name-only", "-z", ref).split("\0")
    names=sorted(set(names)-{""})
    result, omitted = {}, []
    blobs={}
    if ref != 'WORKTREE':
        eligible=[n for n in names if not any(p in SKIP for p in PurePosixPath(n).parts) and not sensitive_path(n) and '\n' not in n and '\r' not in n]
        specs=[f':{n}' if ref=='INDEX' else f'{ref}:{n}' for n in eligible]
        if specs:
            metadata=git(root,'cat-file','--batch-check=%(objectname) %(objecttype) %(objectsize)',stdin=('\n'.join(specs)+'\n').encode()).splitlines()
            objects=[];reserved=0
            for name,line in zip(eligible,metadata):
                parts=line.split()
                if len(parts)!=3 or parts[1]!='blob' or not parts[2].isdigit() or int(parts[2])>1_000_000 or reserved+int(parts[2])>30_000_000:
                    omitted.append(name);continue
                objects.append((name,parts[0]));reserved+=int(parts[2])
            if objects:
                stream=io.BytesIO(git(root,'cat-file','--batch',stdin=('\n'.join(x[1] for x in objects)+'\n').encode(),binary=True))
                for name,_ in objects:
                    header=stream.readline().split();size=int(header[-1]);blobs[name]=stream.read(size);stream.read(1)
    total = 0
    for name in names:
        if sensitive_path(name):omitted.append(name);continue
        if any(p in SKIP for p in PurePosixPath(name).parts):
            continue
        if ref == "WORKTREE":
            p = safe_path(root, name)
            if not p.is_file() or p.is_symlink():
                continue
            if p.stat().st_size > 1_000_000:
                omitted.append(name); continue
            raw = p.read_bytes()
        else:
            if name not in blobs:
                omitted.append(name);continue
            raw=blobs[name]
        if b"\0" in raw or total + len(raw) > 30_000_000:
            omitted.append(name); continue
        try:
            result[name] = raw.decode("utf-8")
        except UnicodeDecodeError:
            omitted.append(name); continue
        total += len(raw)
    return result, omitted


def capture(path, scope):
    root = git(path, "rev-parse", "--show-toplevel").strip()
    mode = scope.get("mode", "branch")
    def rev(ref):
        if not ref or str(ref).startswith("-"):
            raise ValueError("invalid Git ref")
        return git(root, "rev-parse", "--verify", str(ref) + "^{commit}").strip()
    before = git(root, "status", "--porcelain=v1", "-z")
    if mode in {"branch", "range"}:
        head = rev(scope.get("head", "HEAD"))
        base = rev(scope.get("base", "main"))
        if mode == "branch":
            base = git(root, "merge-base", base, head).strip()
        args = [base, head]; head_ref = head
    elif mode in {"staged", "working-tree"}:
        base = rev("HEAD"); head = None
        head_ref = "INDEX" if mode == "staged" else "WORKTREE"
        args = ["--cached", base] if mode == "staged" else [base]
    else:
        raise ValueError("unsupported local review scope")
    selected_paths=scope.get('paths',[])
    if not isinstance(selected_paths,list) or len(selected_paths)>100:raise ValueError('文件筛选必须是最多100项的路径列表')
    for item in selected_paths:
        if not isinstance(item,str) or item.startswith('-'):raise ValueError('无效文件路径')
        safe_path(root,item)
        if sensitive_path(item):raise ValueError('凭证文件不进入模型审查范围：'+item)
    if os.name=='nt' and selected_paths:
        canonical={p.casefold():p for p in git(root,'ls-files','-z').split('\0') if p}
        selected_paths=[canonical.get(p.casefold(),p) for p in selected_paths]
    if selected_paths:args += ['--',*selected_paths]
    diff = git(root, "diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=5", *args)
    old, omitted_old = _files(root, base)
    new, omitted_new = _files(root, head_ref)
    if mode == "working-tree" and not scope.get("include_untracked", False):
        tracked = set(git(root, "ls-files", "-z").split("\0"))
        new = {k: v for k, v in new.items() if k in tracked}
    if mode == "working-tree" and scope.get("include_untracked", False):
        import difflib
        for name in sorted(set(new) - set(old)):
            if name in git(root, "ls-files", "--others", "--exclude-standard").splitlines() and (not selected_paths or name in selected_paths):
                diff += f"diff --git a/{name} b/{name}\n" + "".join(difflib.unified_diff([], new[name].splitlines(True), fromfile="/dev/null", tofile="b/" + name))
    # Re-read mutable contents: status alone cannot detect same-size edits to an already dirty file.
    if mode in {"staged", "working-tree"}:
        again, _ = _files(root, head_ref)
        if mode == "working-tree" and not scope.get("include_untracked", False):
            again = {k: v for k, v in again.items() if k in tracked}
        if digest(new) != digest(again) or before != git(root, "status", "--porcelain=v1", "-z"):
            raise ValueError("repository changed during capture; retry")
        if diff != git(root, "diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=5", *args) and not scope.get("include_untracked"):
            raise ValueError("index changed during capture; retry")
    if not diff.strip():
        raise ValueError("selected scope has no changes")
    chunks=re.split(r'(?m)(?=^diff --git )',diff);safe_chunks=[]
    for chunk in chunks:
        header=chunk.splitlines()[0] if chunk else ''
        try:paths=shlex.split(header.removeprefix('diff --git ')) if header.startswith('diff --git ') else []
        except ValueError:paths=[]
        if any(sensitive_path(p[2:]) for p in paths):continue
        safe_chunks.append(chunk)
    diff=''.join(safe_chunks)
    if not diff.strip():raise ValueError('所选变更仅包含凭证文件，不会发送给模型')
    value = {"repository": root, "scope": scope, "base_sha": base, "head_sha": head,
             "base_files": old, "head_files": new, "diff": diff,
             "omitted_files": sorted(set(omitted_old + omitted_new))}
    value["changed_paths"] = sorted(set(git(root,"diff","--no-ext-diff","--no-textconv","--name-only","-z",*args).split("\0"))-{ "" })
    if mode == "working-tree" and scope.get("include_untracked",False):
        extras=set(new)-set(git(root,"ls-files","-z").split("\0"))
        if selected_paths:extras &= set(selected_paths)
        value["changed_paths"] = sorted(set(value["changed_paths"]) | extras)
    value["diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest()
    value["snapshot_id"] = "snap-" + digest(value)
    value["content_sha256"] = digest({"base": old, "head": new})
    return value


def diff_path(header):
    """Decode Git's C-quoted path or unquoted path + TAB timestamp separator."""
    if header.startswith('"'):
        match=re.match(r'"(?:\\.|[^"\\])*"',header)
        if not match:raise ValueError('malformed quoted Git path')
        value=ast.literal_eval(match.group(0))
        try:value=value.encode('latin1').decode('utf-8')
        except (UnicodeEncodeError,UnicodeDecodeError):pass
    else:value=header.split('\t',1)[0]
    return value[2:] if value.startswith(('a/','b/')) else value


def symbols(path, text):
    if Path(path).suffix in {".py", ".pyi"}:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        result = []
        def walk(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = prefix + child.name
                    result.append({"name": name, "start": child.lineno, "end": child.end_lineno, "kind": type(child).__name__})
                    walk(child, name + ".")
                else:
                    walk(child, prefix)
        walk(tree)
        return result
    if Path(path).suffix == '.vue':
        result=[]
        for script in re.finditer(r'<script\b([^>]*)>([\s\S]*?)</script\s*>',text,re.I):
            suffix='.ts' if re.search(r'lang\s*=\s*[\'"]ts[\'"]',script.group(1),re.I) else '.js'
            offset=text[:script.start(2)].count('\n')
            for item in symbols(path+suffix,script.group(2)):
                result.append({**item,'start':item['start']+offset,'end':item['end']+offset})
        return result
    if Path(path).suffix not in SOURCE:
        return []
    try:
        from tree_sitter import Language, Parser
        if Path(path).suffix in {".ts", ".tsx"}:
            import tree_sitter_typescript as grammar
            lang = grammar.language_tsx() if path.endswith(".tsx") else grammar.language_typescript()
        else:
            import tree_sitter_javascript as grammar
            lang = grammar.language()
        tree = Parser(Language(lang)).parse(text.encode())
        kinds = {"class_declaration", "function_declaration", "method_definition", "interface_declaration", "type_alias_declaration", "enum_declaration", "lexical_declaration"}
        result = []
        def visit(node):
            if node.type in kinds:
                name = node.child_by_field_name("name")
                result.append({"name": name.text.decode() if name else node.text.decode()[:100], "start": node.start_point[0]+1, "end": node.end_point[0]+1, "kind": node.type})
            for child in node.children:
                visit(child)
        visit(tree.root_node)
        return result
    except ImportError:
        return []


def change_units(snapshot):
    """Anchor actual +/- lines, not hunk context. Split large symbols without dropping changes.

    Reference edges are lexical candidates, not a claimed sound call graph.
    """
    units = {}; path = ""; old_path = ""; old_line = new_line = 1; in_hunk = False
    symbol_cache = {}
    for raw in snapshot["diff"].splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
        elif raw.startswith("--- ") and not in_hunk:
            old_path = diff_path(raw[4:])
        elif raw.startswith("+++ ") and not in_hunk:
            path = diff_path(raw[4:])
        elif raw.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
            if match:
                old_line, new_line = int(match.group(1)), int(match.group(3)); in_hunk = True
        elif in_hunk and raw[:1] in {"+", "-", " "}:
            if raw[0] != " ":
                side = "head" if raw[0] == "+" else "base"
                file_path = path if side == "head" else old_path
                line = max(1, new_line if side == "head" else old_line)
                if file_path not in snapshot[side + "_files"]:
                    raise ValueError('changed source unavailable in frozen snapshot: '+file_path)
                text = snapshot[side + "_files"][file_path]
                if not 1 <= line <= len(text.splitlines()):
                    raise ValueError('changed line outside frozen source: '+file_path)
                if (side, file_path) not in symbol_cache:
                    symbol_cache[(side, file_path)] = symbols(file_path, text)
                syms = symbol_cache[(side, file_path)]
                candidates = [s for s in syms if s["start"] <= line <= s["end"]]
                symbol = min(candidates, key=lambda s: s["end"]-s["start"]) if candidates else {"name": "<file>", "start": 1, "end": len(text.splitlines()), "kind": "file"}
                chunk = (line - symbol["start"]) // 120
                key = (file_path, symbol["name"], symbol["start"], side, chunk)
                if key not in units:
                    start = max(1, symbol["start"] + chunk*120 - 3)
                    end = min(len(text.splitlines()),symbol["end"]+3, start+126)
                    if end < start:raise ValueError('invalid changed context range: '+file_path)
                    context = "\n".join(text.splitlines()[start-1:end])
                    units[key] = {"path": file_path, "side": side, "symbol": symbol, "changed_lines": [],
                        "context_start": start, "context_end": end, "context": context[:20000],
                        "context_truncated": len(context)>20000, "hunks": []}
                    units[key]['context_blank']=not context.strip()
                units[key]["changed_lines"].append(line)
                units[key]["hunks"].append(raw)
            if raw[0] != "+": old_line += 1
            if raw[0] != "-": new_line += 1
    result = []
    for unit in units.values():
        unit["change_unit_id"] = "cu-" + digest([snapshot["diff_sha256"], unit["path"], unit["side"], unit["symbol"], unit["changed_lines"]])[:20]
        name = unit["symbol"]["name"].split(".")[-1]
        references = []
        if name != "<file>" and re.fullmatch(r"\w+", name):
            pattern = re.compile(r"\b" + re.escape(name) + r"\b")
            for target, text in snapshot["head_files"].items():
                for n, source in enumerate(text.splitlines(), 1):
                    if pattern.search(source):
                        references.append({"path": target, "line": n, "kind": "test_candidate" if re.search(r"(^|[/_.-])(test|tests|spec)([/_.-]|$)", target) else "reference_candidate", "precision": "lexical"})
                        if len(references)>=40: break
                if len(references)>=40: break
        unit["reference_candidates"] = references
        unit["references_may_be_truncated"] = len(references)>=40
        result.append(unit)
    # Renames, file-mode and binary changes may contain no +/- lines at all.
    covered = {u["path"] for u in result}
    for path in snapshot.get("changed_paths",[]):
        if path in covered: continue
        side = "head" if path in snapshot["head_files"] else "base"
        text = snapshot[side+"_files"].get(path,"")
        result.append({"change_unit_id":"cu-"+digest([snapshot["diff_sha256"],path,"metadata"])[:20],
            "path":path,"side":side,"symbol":{"name":"<file-metadata>","start":1,"end":len(text.splitlines()),"kind":"file_metadata"},
            "changed_lines":[],"context":text[:20000],"context_start":1,"context_truncated":len(text)>20000,
            "reference_candidates":[],"hunks":[],"limitation":"No changed source lines (metadata, rename or unavailable/binary content); review coverage is limited"})
    return result
