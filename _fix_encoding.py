import pathlib
import os

files = [
    'bots/dsp_bot.py', 'bots/idx_bot.py', 'bots/up_bot.py', 'bots/mon_bot.py',
    'bots/admin_bot/handlers.py', 'bots/admin_bot/conversation.py', 'bots/admin_bot/display.py',
    'database/session.py', 'database/cache.py', 'services/code_generator.py',
    'services/permission.py', 'services/mon/scheduler.py', 'services/db_backup.py',
    'storage/delivery_resolver.py', 'utils/force_join.py', 'utils/task_utils.py',
    'utils/monitor.py', 'utils/file_utils.py', 'admin/__init__.py', 'admin/seed_topology.py',
    'config/generate_topology.py', 'run_all.py',
]

replacements = {
    '\uff0c': ',',   # full-width comma
    '\uff08': '(',   # full-width left paren
    '\uff09': ')',   # full-width right paren
    '\uff1a': ':',   # full-width colon
    '\uff0e': '.',   # full-width period
    '\uff01': '!',   # full-width exclamation
    '\uff1f': '?',   # full-width question
    '\u201c': '"',   # left double quote
    '\u201d': '"',   # right double quote
    '\u2018': "'",   # left single quote
    '\u2019': "'",   # right single quote
}

for fname in files:
    p = pathlib.Path(fname)
    if not p.exists():
        continue
    content = p.read_text('utf-8')
    changed = False
    for old, new in replacements.items():
        if old in content:
            content = content.replace(old, new)
            changed = True
    if changed:
        p.write_text(content, 'utf-8')
        print(f'FIXED: {fname}')
print('DONE')