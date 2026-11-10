# -*- coding: utf-8 -*-
"""檢查 dashboard.py 中所有「模組別名.屬性」呼叫是否真的存在，預防 AttributeError。

用法：
    C:/Users/singk/anaconda3/python.exe tools/check_module_attrs.py              # 檢查 dashboard.py
    C:/Users/singk/anaconda3/python.exe tools/check_module_attrs.py other.py     # 檢查其他檔案

做法：以 AST 找出 `from tools import X as alias` / `import tools.X as alias`，再收集所有 `alias.attr`，
實際 import 模組後逐一 hasattr 核對。全部存在則結束碼 0，否則列出缺少項目並以 1 結束。
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import ast
import importlib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def module_aliases(tree):
    """回傳 {別名: 模組完整名稱}，只收 tools 套件內的模組。"""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools":
            for a in node.names:
                aliases[a.asname or a.name] = f"tools.{a.name}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("tools.") and a.asname:
                    aliases[a.asname] = a.name
    return aliases


def attribute_uses(tree, aliases):
    """回傳 {別名: {屬性: [行號,...]}}。"""
    uses = defaultdict(lambda: defaultdict(list))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            uses[node.value.id][node.attr].append(node.lineno)
    return uses


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dashboard.py"
    tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
    aliases = module_aliases(tree)
    uses = attribute_uses(tree, aliases)
    missing, total = [], 0
    for alias in sorted(uses):
        module = importlib.import_module(aliases[alias])
        attrs = uses[alias]
        ok = [a for a in sorted(attrs) if hasattr(module, a)]
        bad = [a for a in sorted(attrs) if not hasattr(module, a)]
        total += len(attrs)
        print(f"{'✅' if not bad else '❌'} {alias} → {aliases[alias]}：{len(ok)}/{len(attrs)} 個屬性存在")
        print("    " + "、".join(f"{a}" for a in ok))
        for a in bad:
            missing.append(f"{alias}.{a}（第 {', '.join(map(str, attrs[a]))} 行）")
    print(f"\n共檢查 {target.name} 的 {len(uses)} 個模組別名、{total} 個不重複屬性。")
    if missing:
        print("❌ 缺少：" + "；".join(missing))
        sys.exit(1)
    print("✅ 全部存在，不會發生 AttributeError。")


if __name__ == "__main__":
    main()
