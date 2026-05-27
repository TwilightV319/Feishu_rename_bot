#!/usr/bin/env python3
"""
Helper script for invoice/payment screenshot renaming.
Handles file validation, safe filename generation, and renaming operations.
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are illegal in filenames."""
    # Replace common illegal chars with underscore
    illegal = r'[<>:"/\\|?*\x00-\x1f]'
    name = re.sub(illegal, '_', name)
    # Remove leading/trailing whitespace and dots
    name = name.strip(' .')
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    # Limit length
    if len(name) > 100:
        name = name[:100]
    return name


def parse_amount(amount_str: str) -> str:
    """Normalize amount string to a standard format like 128.50."""
    # Remove currency symbols and whitespace
    s = amount_str.replace('¥', '').replace('￥', '').replace('$', '').replace(',', '').replace('元', '').strip()
    try:
        f = float(s)
        # Format with 2 decimal places if it has cents, otherwise integer
        if f == int(f):
            return str(int(f))
        return f"{f:.2f}"
    except ValueError:
        return sanitize_filename(s)


def _detect_extension_from_content(path: Path) -> str:
    """Detect file extension from magic bytes or PIL when suffix is missing."""
    # PDF check
    try:
        with open(path, "rb") as f:
            header = f.read(5)
            if header == b"%PDF-":
                return ".pdf"
    except Exception:
        pass

    # Image check via PIL
    try:
        from PIL import Image
        with Image.open(path) as img:
            fmt_map = {
                "JPEG": ".jpg", "PNG": ".png", "GIF": ".gif",
                "BMP": ".bmp", "WEBP": ".webp", "TIFF": ".tiff",
            }
            return fmt_map.get(img.format, ".jpg")
    except Exception:
        pass

    return ".jpg"


def build_new_filename(item_name: str, doc_type: str, amount: str, original_path: str) -> str:
    """Build the new filename in format: 物品名称_发票/付款截图_金额.ext"""
    path = Path(original_path)
    ext = path.suffix.lower()

    # Detect extension from file content if missing
    if not ext and path.exists():
        ext = _detect_extension_from_content(path)

    safe_name = sanitize_filename(item_name)
    safe_type = sanitize_filename(doc_type)
    safe_amount = parse_amount(amount)

    new_name = f"{safe_name}_{safe_type}_{safe_amount}{ext}"
    return new_name


def rename_file(original_path: str, new_name: str, output_dir: str = None, dry_run: bool = False) -> str:
    """Rename a file. Returns the new path."""
    orig = Path(original_path).resolve()
    if not orig.exists():
        raise FileNotFoundError(f"File not found: {original_path}")
    
    if output_dir:
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = orig.parent
    
    new_path = out / new_name
    
    # Handle duplicate names
    counter = 1
    stem = new_path.stem
    suffix = new_path.suffix
    while new_path.exists():
        new_path = out / f"{stem}_{counter}{suffix}"
        counter += 1
    
    if dry_run:
        print(f"[DRY-RUN] Would rename:\n  {orig}\n  -> {new_path}")
    else:
        shutil.move(str(orig), str(new_path))
        print(f"Renamed:\n  {orig}\n  -> {new_path}")
    
    return str(new_path)


def validate_files(file_paths: list[str]) -> list[str]:
    """Filter and validate input files. Returns list of valid paths."""
    supported = {'.pdf', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff'}
    valid = []
    for p in file_paths:
        path = Path(p)
        if not path.exists():
            print(f"Warning: file not found, skipping: {p}", file=sys.stderr)
            continue
        if path.suffix.lower() not in supported:
            print(f"Warning: unsupported file type ({path.suffix}), skipping: {p}", file=sys.stderr)
            continue
        valid.append(str(path.resolve()))
    return valid


def main():
    parser = argparse.ArgumentParser(description="Rename invoice/payment files.")
    sub = parser.add_subparsers(dest="command")
    
    # validate command
    val = sub.add_parser("validate", help="Validate input files")
    val.add_argument("files", nargs="+", help="Input file paths")
    
    # rename command
    ren = sub.add_parser("rename", help="Rename a file")
    ren.add_argument("--item", required=True, help="Item name")
    ren.add_argument("--type", required=True, help="Document type (e.g., 发票, 付款截图)")
    ren.add_argument("--amount", required=True, help="Amount")
    ren.add_argument("--input", required=True, help="Original file path")
    ren.add_argument("--output-dir", default=None, help="Output directory")
    ren.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    
    # batch command
    batch = sub.add_parser("batch", help="Batch rename from a mapping file")
    batch.add_argument("mapping", help="Path to mapping file (original_path|new_name per line)")
    batch.add_argument("--output-dir", default=None, help="Output directory")
    batch.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    
    args = parser.parse_args()
    
    if args.command == "validate":
        valid = validate_files(args.files)
        for v in valid:
            print(v)
        sys.exit(0 if valid else 1)
    
    elif args.command == "rename":
        new_name = build_new_filename(args.item, args.type, args.amount, args.input)
        new_path = rename_file(args.input, new_name, args.output_dir, args.dry_run)
        print(new_path)
    
    elif args.command == "batch":
        with open(args.mapping, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|", 1)
                if len(parts) != 2:
                    continue
                orig, new_name = parts[0].strip(), parts[1].strip()
                rename_file(orig, new_name, args.output_dir, args.dry_run)
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
