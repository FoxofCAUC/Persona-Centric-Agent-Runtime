"""Render QQ transport as plain chat text, preserving the semantic source.

This is presentation conversion, not model repair or another generation. Code
contents, numbers and URLs remain data; Markdown decoration is not sent to QQ.
"""
import re


def is_code_line(line):
    return bool(re.match(r'^\s*(?:def\s+\w+\(|class\s+\w+|(?:from|import)\s+\w+|return\s+|assert\s+|print\(|[A-Za-z_]\w*(?:\[[^\n]+?\])?\s*=(?!=))',line))


def plain_qq_text(text):
    # Protect code contents from emphasis stripping (e.g. Python ** and a_b).
    protected = []

    def protect(value):
        protected.append(value)
        return f'\x00{len(protected)-1}\x00'

    lines = []
    fence = None
    code = []
    for line in text.split('\n'):
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                lines.append(protect('\n'.join(code))); code = []; fence = None
            else:
                code.append(line)
        elif match:
            fence = match[1]
        elif is_code_line(line):
            lines.append(protect(line))
        else:
            lines.append(line)
    if fence:
        lines.append(protect('\n'.join(code)))  # Remove an unfinished fence, preserve its body.
    result = '\n'.join(lines)
    result = re.sub(r'(`+)([^`\n]+?)\1', lambda m: protect(m[2]), result)
    # References are not dereferenced. Visible labels and target URLs survive.
    result = re.sub(r'!?\[([^\]\n]*)\]\(([^\n]*?)\)', lambda m: f'{m[1]}（{m[2]}）' if m[1] else m[2], result)
    result = re.sub(r'(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$', r'\1', result)
    result = re.sub(r'(?m)^[ \t]{0,3}(?:>[ \t]*)+', '', result)
    result = re.sub(r'(?m)^[ \t]*[-+*][ \t]+', '', result)
    result = re.sub(r'(?m)^[ \t]*(?:[-*_][ \t]*){3,}$', '', result)
    # Convert pipe tables into readable rows; remove alignment-only rows.
    rows = result.split('\n')
    for i, line in enumerate(rows):
        if '|' in line and re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', line):
            rows[i] = ''
            if i:
                rows[i-1] = '；'.join(cell.strip() for cell in rows[i-1].strip().strip('|').split('|'))
            j = i+1
            while j < len(rows) and '|' in rows[j] and rows[j].strip():
                rows[j] = '；'.join(cell.strip() for cell in rows[j].strip().strip('|').split('|')); j += 1
    result = '\n'.join(rows)
    # URL path/query characters are data, including underscores and asterisks.
    result = re.sub(r'https?://[^\s（）<>]+', lambda m: protect(m[0]), result)
    result = re.sub(r'\b[A-Za-z0-9_]+\s+\*\*\s+[A-Za-z0-9_]+',lambda m:protect(m[0]),result)
    result = re.sub(r'\\([*_`~#>\[\]])', r'\1', result)
    for marker in ('**', '__', '~~'):
        result = re.sub(re.escape(marker)+r'([^\n]+?)'+re.escape(marker), r'\1', result)
    result = re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)', r'\1', result)
    result = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'\1', result)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return re.sub(r'\x00(\d+)\x00', lambda m: protected[int(m[1])], result)
