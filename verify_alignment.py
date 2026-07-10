import re
import sys

readmes = {
    "en": "README_en.md",
    "zh-TW": "README.md",
    "zh-CN": "README_zh-CN.md",
    "ja": "README_ja.md"
}

def get_structural_signature(line):
    line_strip = line.strip()
    if not line_strip:
        return "EMPTY"
    if line_strip.startswith("#"):
        return f"HEADER:{len(re.match(r'^(#+)', line_strip).group(1))}"
    if line_strip.startswith("```"):
        return f"CODE_BLOCK:{line_strip}"
    if line_strip.startswith(("- ", "* ")):
        return "BULLET_LIST"
    if re.match(r"^\d+\.\s+", line_strip):
        return "NUMBER_LIST"
    if line_strip.startswith("|") and line_strip.endswith("|"):
        return f"TABLE_ROW:{line_strip.count('|')}"
    links_count = len(re.findall(r"\[.*?\]\(.*?\)", line))
    backticks_count = line.count("`")
    return f"TEXT:links={links_count}:backticks={backticks_count}"

# Load contents
contents = {}
for lang, filename in readmes.items():
    with open(filename, "r", encoding="utf-8") as f:
        contents[lang] = f.read().splitlines()

# Perform comparison
en_sigs = [get_structural_signature(l) for l in contents["en"]]
mismatches = False

for lang in ["zh-TW", "zh-CN", "ja"]:
    lang_lines = contents[lang]
    if len(lang_lines) != len(en_sigs):
        print(f"❌ Line count mismatch for {lang}: {len(lang_lines)} vs {len(en_sigs)} (en)")
        mismatches = True
        continue
    
    for idx, (line, en_sig) in enumerate(zip(lang_lines, en_sigs)):
        lang_sig = get_structural_signature(line)
        if en_sig != lang_sig:
            print(f"❌ Structural mismatch in {lang} at line {idx+1}")
            print(f"   EN: {contents['en'][idx]}")
            print(f"   {lang}: {line}")
            mismatches = True

if mismatches:
    sys.exit(1)
else:
    print("✅ All translation files are 100% isomorphic and structurally aligned!")
    sys.exit(0)