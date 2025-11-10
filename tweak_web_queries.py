#!/usr/bin/env python3
"""
tweak_web_queries.py

CLI to tweak web_queries JSON configs (like configs/web_queries.generic.json).

Features:
- List categories & queries
- Set global defaults (recency_days, max_results)
- Add/Remove categories
- Add/Remove/Rename queries within a category
- Per-category overrides (recency_days/max_results)
- Choose which "version" block to edit (defaults to highest version found)
- Dry-run + automatic .bak backup

Usage examples (see bottom or run with -h).
"""

import argparse, json, sys, shutil, pathlib
from typing import Any, Dict, List, Optional

def load_json(path: pathlib.Path) -> List[Dict[str, Any]]:
    """
    Some files (like yours) may contain multiple JSON objects back-to-back.
    We try to parse them sequentially.
    """
    text = path.read_text(encoding="utf-8").strip()
    objs: List[Dict[str, Any]] = []
    i = 0
    while i < len(text):
        # Skip leading whitespace
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        # Find a JSON object starting at i
        if text[i] != '{':
            raise ValueError("Expected '{' at position %d while parsing concatenated JSON." % i)
        # Use a simple brace counter to find the matching end
        depth = 0
        j = i
        in_str = False
        esc = False
        while j < len(text):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
            j += 1
        obj_text = text[i:j]
        try:
            objs.append(json.loads(obj_text))
        except Exception as e:
            raise ValueError(f"Failed to parse JSON block starting at {i}: {e}") from e
        i = j
    if not objs:
        raise ValueError("No JSON objects found in file.")
    return objs

def pick_version(objs: List[Dict[str, Any]], version: Optional[str]) -> Dict[str, Any]:
    # Choose the object with the highest numeric version if not specified
    if version is None:
        candidates = []
        for o in objs:
            v = o.get("version")
            try:
                vn = float(v) if v is not None else -1
            except:
                vn = -1
            candidates.append((vn, o))
        chosen = sorted(candidates, key=lambda t: t[0])[-1][1]
        return chosen
    # else find exact match
    for o in objs:
        if str(o.get("version")) == str(version):
            return o
    raise ValueError(f"Version {version} not found. Available: {[o.get('version') for o in objs]}")

def list_categories(cfg: Dict[str, Any]) -> None:
    cats = cfg.get("categories", [])
    for idx, cat in enumerate(cats):
        name = cat.get("name", f"(unnamed-{idx})")
        qn = len(cat.get("queries", []))
        extras = []
        if "recency_days" in cat: extras.append(f"recency_days={cat['recency_days']}")
        if "max_results"  in cat: extras.append(f"max_results={cat['max_results']}")
        extra_str = f" [{' '.join(extras)}]" if extras else ""
        print(f"[{idx}] {name} — {qn} queries{extra_str}")

def list_queries(cfg: Dict[str, Any], category_index: int) -> None:
    cats = cfg.get("categories", [])
    if not (0 <= category_index < len(cats)):
        raise IndexError(f"Category index out of range (0..{len(cats)-1})")
    cat = cats[category_index]
    name = cat.get("name", f"(unnamed-{category_index})")
    print(f"Category [{category_index}] {name}")
    for i, q in enumerate(cat.get("queries", [])):
        print(f"  ({i}) {q}")

def set_defaults(cfg: Dict[str, Any], recency_days: Optional[int], max_results: Optional[int]) -> None:
    defaults = cfg.setdefault("defaults", {})
    if recency_days is not None:
        defaults["recency_days"] = recency_days
    if max_results is not None:
        defaults["max_results"] = max_results

def ensure_category(cfg: Dict[str, Any], category_index: Optional[int], category_name: Optional[str]) -> int:
    cats = cfg.setdefault("categories", [])
    if category_index is not None:
        if not (0 <= category_index < len(cats)):
            raise IndexError(f"Category index out of range (0..{len(cats)-1})")
        return category_index
    # find by name
    if category_name is not None:
        for i, c in enumerate(cats):
            if c.get("name") == category_name:
                return i
        # if not found, create it
        cats.append({"name": category_name, "queries": []})
        return len(cats) - 1
    raise ValueError("Must provide either --category-index or --category-name")

def rename_category(cfg: Dict[str, Any], idx: int, new_name: str) -> None:
    cats = cfg.get("categories", [])
    cats[idx]["name"] = new_name

def add_query(cfg: Dict[str, Any], idx: int, query: str, position: Optional[int]) -> None:
    cats = cfg.get("categories", [])
    queries = cats[idx].setdefault("queries", [])
    if position is None or position < 0 or position > len(queries):
        queries.append(query)
    else:
        queries.insert(position, query)

def remove_query(cfg: Dict[str, Any], idx: int, qindex: int) -> None:
    cats = cfg.get("categories", [])
    queries = cats[idx].get("queries", [])
    if not (0 <= qindex < len(queries)):
        raise IndexError(f"Query index out of range (0..{len(queries)-1})")
    queries.pop(qindex)

def set_category_overrides(cfg: Dict[str, Any], idx: int, recency_days: Optional[int], max_results: Optional[int]) -> None:
    cats = cfg.get("categories", [])
    if recency_days is not None:
        cats[idx]["recency_days"] = recency_days
    if max_results is not None:
        cats[idx]["max_results"] = max_results

def add_category(cfg: Dict[str, Any], name: str) -> int:
    cats = cfg.setdefault("categories", [])
    cats.append({"name": name, "queries": []})
    return len(cats) - 1

def remove_category(cfg: Dict[str, Any], idx: int) -> None:
    cats = cfg.get("categories", [])
    if not (0 <= idx < len(cats)):
        raise IndexError(f"Category index out of range (0..{len(cats)-1})")
    cats.pop(idx)

def save_json(path: pathlib.Path, objs: List[Dict[str, Any]], edited_obj: Dict[str, Any], version_to_write: str, dry_run: bool) -> None:
    # Replace the chosen version block in objs
    replaced = False
    for i, o in enumerate(objs):
        if str(o.get("version")) == str(version_to_write):
            objs[i] = edited_obj
            replaced = True
            break
    if not replaced:
        # If the chosen version didn't exist originally (unlikely), append
        objs.append(edited_obj)

    serialized = "\n".join(json.dumps(o, indent=2, ensure_ascii=False) for o in objs) + "\n"
    if dry_run:
        print("=== DRY RUN OUTPUT ===")
        print(serialized)
        return

    # Backup original
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    path.write_text(serialized, encoding="utf-8")
    print(f"Saved. Backup written to: {backup}")

def main():
    ap = argparse.ArgumentParser(description="Tweak web_queries JSON config.")
    ap.add_argument("file", help="Path to web_queries JSON (e.g., configs/web_queries.generic.json)")
    ap.add_argument("--version", help="Version string to edit (default: highest in file)")
    ap.add_argument("--dry-run", action="store_true", help="Do not write; print result to STDOUT")

    # Inspect
    ap.add_argument("--list-categories", action="store_true", help="List categories")
    ap.add_argument("--list-queries", type=int, metavar="CAT_IDX", help="List queries in category index")

    # Defaults
    ap.add_argument("--set-default-recency", type=int, help="Set defaults.recency_days")
    ap.add_argument("--set-default-max", type=int, help="Set defaults.max_results")

    # Category management
    ap.add_argument("--add-category", metavar="NAME", help="Add a new category with this name")
    ap.add_argument("--remove-category", type=int, metavar="CAT_IDX", help="Remove category by index")
    ap.add_argument("--rename-category", nargs=2, metavar=("CAT_IDX", "NEW_NAME"), help="Rename category index to NEW_NAME")

    # Per-category overrides
    ap.add_argument("--category-index", type=int, help="Category index for query ops / overrides")
    ap.add_argument("--category-name", help="Category name (create if missing) for query ops / overrides")
    ap.add_argument("--set-category-recency", type=int, help="Set per-category recency_days")
    ap.add_argument("--set-category-max", type=int, help="Set per-category max_results")

    # Query edits
    ap.add_argument("--add-query", metavar="QUERY", help="Add a query to target category")
    ap.add_argument("--add-query-pos", type=int, help="Insert added query at position (default: append)")
    ap.add_argument("--remove-query", type=int, metavar="Q_IDX", help="Remove query by index in category")

    args = ap.parse_args()
    path = pathlib.Path(args.file).expanduser()
    objs = load_json(path)
    cfg = pick_version(objs, args.version)
    edit_version = str(cfg.get("version"))

    # Simple readable actions (that do not modify)
    if args.list_categories:
        list_categories(cfg)
        if not any([
            args.set_default_recency, args.set_default_max, args.add_category,
            args.remove_category, args.rename_category, args.add_query,
            args.remove_query, args.set_category_recency, args.set_category_max
        ]):
            return
    if args.list_queries is not None:
        list_queries(cfg, args.list_queries)
        if not any([
            args.set_default_recency, args.set_default_max, args.add_category,
            args.remove_category, args.rename_category, args.add_query,
            args.remove_query, args.set_category_recency, args.set_category_max
        ]):
            return

    # Mutations
    changed = False

    if args.set_default_recency is not None or args.set_default_max is not None:
        set_defaults(cfg, args.set_default_recency, args.set_default_max)
        changed = True

    if args.add_category:
        add_category(cfg, args.add_category)
        changed = True

    if args.remove_category is not None:
        remove_category(cfg, args.remove_category)
        changed = True

    if args.rename_category:
        ci = int(args.rename_category[0])
        new_name = args.rename_category[1]
        rename_category(cfg, ci, new_name)
        changed = True

    # Category-specific operations (ensure target)
    target_idx = None
    if any([args.add_query, args.remove_query is not None,
            args.set_category_recency is not None, args.set_category_max is not None,
            args.category_name is not None or args.category_index is not None]):
        target_idx = ensure_category(cfg, args.category_index, args.category_name)

    if args.set_category_recency is not None or args.set_category_max is not None:
        set_category_overrides(cfg, target_idx, args.set_category_recency, args.set_category_max)
        changed = True

    if args.add_query:
        add_query(cfg, target_idx, args.add_query, args.add_query_pos)
        changed = True

    if args.remove_query is not None:
        remove_query(cfg, target_idx, args.remove_query)
        changed = True

    # Save if changed
    if changed:
        save_json(path, objs, cfg, edit_version, args.dry_run)
    else:
        # No changes requested; if only list was used, we’ve already printed
        if not (args.list_categories or args.list_queries is not None):
            print("No changes requested. Use -h for options.")

if __name__ == "__main__":
    main()