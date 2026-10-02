#!/usr/bin/env python3
"""Parse mysqldump extended INSERTs for one table, print TSV. Read-only.

Usage: dump_table.py <table> [col1,col2] [path/to/dump.sql]
Defaults to $CHANZHI_SQL / config.SRC_SQL.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from config import SRC_SQL as _DEFAULT_SQL
except ImportError:  # standalone copy without config.py
    _DEFAULT_SQL = os.environ.get('CHANZHI_SQL', '')

SQL = _DEFAULT_SQL


def sql_unescape(s):
    """MySQL 字符串反转义: \\ \" \' \n \r \t \0."""
    mp = {'\\': '\\', "'": "'", '"': '"', 'n': '\n', 'r': '\r', 't': '\t', '0': '\0'}
    out = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            out.append(mp.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)


def split_tuples(vals):
    """Split VALUES(...) into list of field-lists. NULL stays 'NULL'."""
    rows = []
    depth = 0
    inq = False
    esc = False
    cur = ''
    tup = None
    for ch in vals:
        if inq:
            if esc:
                esc = False
                cur += ch
            elif ch == '\\':
                esc = True
                cur += ch
            elif ch == "'":
                inq = False
            else:
                cur += ch
        else:
            if ch == "'":
                inq = True
            elif ch == '(':
                depth += 1
                if depth == 1:
                    tup = []
                    cur = ''
                else:
                    cur += ch
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    tup.append(cur)
                    rows.append(tup)
                    tup = None
                else:
                    cur += ch
            elif ch == ',' and depth == 1:
                tup.append(cur)
                cur = ''
            elif depth >= 1:
                cur += ch
    return rows


def main(table, cols=None, sqlpath=None):
    raw = open(sqlpath or SQL, encoding='utf-8', errors='replace').read()
    m = re.search(r'CREATE TABLE `%s` \((.*?)\) ENGINE' % table, raw, re.S)
    names = re.findall(r'^\s*`(\w+)`', m.group(1), re.M)
    print('# ' + '\t'.join(names))
    for s in raw.split(';\n'):
        if s.startswith('INSERT INTO `%s`' % table):
            vals = s.split('VALUES', 1)[1]
            for tup in split_tuples(vals):
                d = dict(zip(names, tup))
                if cols:
                    print('\t'.join(d.get(c, '') for c in cols))
                else:
                    print('\t'.join(tup))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2].split(',') if len(sys.argv) > 2 and sys.argv[2] else None,
         sys.argv[3] if len(sys.argv) > 3 else None)
