"""Smoke test for hermes_cli/quota_state.py"""
import sys, os, time
sys.path.insert(0, '.')

from hermes_cli.quota_state import (
    QuotaGateState,
    write_wall, read_state, delete_wall, set_release_all,
    sweep_expired, list_walls,
    write_bypass, read_bypass, clear_bypass,
    get_db_path,
)

db = get_db_path()
if db.exists():
    db.unlink()

now = time.time()

# Write a wall
write_wall('custom:modelark', reset_at=now + 3600, window='5h', source='first-429')
print('after write:', list_walls())

# Read state
state = read_state()
print('is_closed:', state.is_closed, '  walled:', state.walled_provider, '  reset_at:', state.reset_at)
print('release_all:', state.release_all)
print('skip_spawn:', state.skip_spawn)

# Set release_all
set_release_all('custom:modelark', True)
state2 = read_state()
print('release_all after set:', state2.release_all)

# Delete wall
delete_wall('custom:modelark')
state3 = read_state()
print('is_closed after delete:', state3.is_closed)

# Cost-window bypass
write_bypass(now + 7200)
print('bypass:', read_bypass())
clear_bypass()
print('bypass after clear:', read_bypass())

# Sweep expired
write_wall('test:expired', reset_at=now - 10)
print('walls before sweep:', len(list_walls()))
swept = sweep_expired()
print('swept:', swept, '  walls after:', len(list_walls()))

# Multi-provider
write_wall('p1', reset_at=now + 100)
write_wall('p2', reset_at=now + 200)
write_wall('p3', reset_at=now + 50)
set_release_all('p2', True)
s = read_state()
print('multi-provider: closed=', s.is_closed, '  primary=', s.walled_provider, '  release_all=', s.release_all)
assert s.release_all == True, 'release_all should be True when any provider has it'
assert s.walled_provider == 'p2', 'primary should be the one with latest reset_at'

# Cleanup
for p in ['p1', 'p2', 'p3']:
    delete_wall(p)
print('after cleanup:', list_walls())

# Fallback chain
state_fb = read_state(fallback_chain=[{'provider': 'deepseek', 'model': 'deepseek-v4-flash'}])
print('fallback model:', state_fb.fallback_model, '  provider:', state_fb.fallback_provider)

# Open gate (no walls)
state_open = read_state()
print('open gate is_closed:', state_open.is_closed)

# is_exempt tests
gs = QuotaGateState(is_closed=True, walled_provider='custom:modelark')
assert gs.is_exempt('deepseek') == True, 'different provider is exempt'
assert gs.is_exempt('custom:modelark') == False, 'same provider is not exempt'
assert gs.is_exempt(None) == False, 'no override is not exempt'
assert gs.is_exempt('') == False, 'empty string is not exempt'
print('is_exempt: OK')

# skip_spawn
gs_no_fb = QuotaGateState(is_closed=True)
assert gs_no_fb.skip_spawn == True, 'no fallback = skip'
gs_with_fb = QuotaGateState(is_closed=True, fallback_model='m', fallback_provider='p')
assert gs_with_fb.skip_spawn == False, 'has fallback = no skip'
gs_open = QuotaGateState(is_closed=False)
assert gs_open.skip_spawn == False, 'open gate = no skip'
print('skip_spawn: OK')

print('ALL OK')
