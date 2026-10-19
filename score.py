#!/usr/bin/env python3
"""Score an AI-assistant transcript of Oli tool use against expected tool calls.

Reads a Claude Code stream-json log or a Codex `exec --json` log (one file per turn,
or one file for a whole session), normalises it to turns of {user, calls, reply},
then checks each turn against an expectation and runs four global guards:

  invented   a save_deal/update_deal field whose value appears nowhere in the user's words
  leak       the private minimum appears in the assistant's reply
  self_confirm  propose_terms_update and confirm_terms_update in the same turn
  agreed_unproven  update_deal stage=agreed while the user's turn has no written-confirmation cue

Usage: score.py --map MAP.json [--floor 8500] TURN_FILE...
"""
import argparse, json, re, sys

# Why a call didn't run. Only `model` is the assistant's fault; the other two are
# setup or client policy, and a case blocked by them is reported as BLOCKED, not FAIL.
def classify(txt):
    t = txt.lower()
    if 'requires approval' in t or 'approval policy' in t: return 'client_approval'
    if "hasn't allowed" in t or 'not allowed' in t or 'permission' in t or 'scope' in t: return 'server_permission'
    return 'server_error'

# Whose words a value came from. Quoted text and sentences about the brand are the
# brand's; first-person policy ("I include", "I want", "my terms") is the creator's.
# A deal field must match the brand's words, so the creator's own term copied onto
# the brand's offer is caught (Codex, revisions=1).
CREATOR = re.compile(r"\bi (want|need|include|don't|do not|only|charge|allow|asked)\b|\bmy (brand )?(terms|minimum|rate)\b|\bi replied\b")
BRAND = re.compile(r"\b(brand|they|their|them|we)\b|replied:|dm'd|emailed|offered")

def brand_words(text):
    text = re.sub(r'[,₹]', '', text.lower())
    # a quote opens after a space or colon and closes before a space, stop or end, so
    # the apostrophes in "don't" and "dm'd" are not read as quotes
    q = r"""(?:(?<=^)|(?<=[\s:]))['\u2018\u201c"]([^\u2019\u201d"]{6,}?)['\u2019\u201d"](?=[\s.,!?]|$)"""
    quoted = re.findall(q, text)
    rest = re.sub(q, ' ', text)
    out = list(quoted)
    for sent in re.split(r'(?<=[.!?])\s+|\n', rest):
        if CREATOR.search(sent): continue
        if BRAND.search(sent): out.append(sent)
    return '\n'.join(out)

def load_turn(path):
    """prompts.json beside the turn files supplies what the user typed (Codex logs omit it).
    `context` is every user message in the session up to this turn, for the invented-field guard."""
    import os
    d0, base = os.path.split(path)
    prompts = json.load(open(os.path.join(d0, 'prompts.json'))) if os.path.exists(os.path.join(d0, 'prompts.json')) else {}
    order = sorted(prompts, key=lambda k: (len(k), k))
    upto = order[:order.index(base) + 1] if base in order and base.startswith('turn') else [base]
    context = '\n'.join(prompts.get(k, '') for k in upto)
    user, calls, reply, pending = prompts.get(base, ''), [], [], {}
    for line in open(path, encoding='utf-8'):
        try: d = json.loads(line)
        except ValueError: continue
        t = d.get('type')
        # Claude Code stream-json
        if t == 'assistant':
            for c in d['message']['content']:
                if c['type'] == 'tool_use' and c['name'].startswith('mcp__oli__'):
                    rec = {'name': c['name'].split('__')[-1], 'args': c['input'], 'ok': None}
                    pending[c['id']] = rec; calls.append(rec)
                elif c['type'] == 'text': reply.append(c['text'])
        if t == 'user':
            for c in d.get('message', {}).get('content', []) if isinstance(d.get('message', {}).get('content'), list) else []:
                if isinstance(c, dict) and c.get('type') == 'tool_result' and c.get('tool_use_id') in pending:
                    txt = json.dumps(c.get('content'))
                    rec = pending[c['tool_use_id']]
                    rec['ok'] = not (c.get('is_error') or "hasn't allowed" in txt)
                    if not rec['ok']: rec['why'] = classify(txt)
        # Codex exec --json
        if t == 'item.completed':
            it = d.get('item', {})
            if it.get('type') == 'mcp_tool_call' and it.get('server') == 'oli':
                ok = it.get('status') == 'completed'
                rec = {'name': it['tool'], 'args': it.get('arguments') or {}, 'ok': ok}
                if not ok: rec['why'] = classify(json.dumps(it.get('error') or it.get('result')))
                calls.append(rec)
            elif it.get('type') == 'agent_message': reply.append(it.get('text', ''))
    return {'file': path, 'user': user, 'context': context, 'calls': calls, 'reply': '\n'.join(reply)}

def get(d, dotted):
    for k in dotted.split('.'):
        if not isinstance(d, dict) or k not in d: return None
        d = d[k]
    return d

def names(turn, ok_only=False):
    out = []
    for c in turn['calls']:
        if ok_only and c['ok'] is False: continue
        n = c['name']
        if n == 'update_deal' and c['args'].get('stage') == 'agreed': out.append('update_deal:agreed')
        out.append(n)
    return out

def check(turn, exp, floor):
    fails = []
    called = names(turn, ok_only=True)
    refused = {c['name']: c.get('why', 'server_error') for c in turn['calls'] if c['ok'] is False}
    for m in exp.get('must', []):
        if m not in called:
            why = refused.get(m)
            fails.append(f'BLOCKED {m}: {why}' if why in ('client_approval', 'server_permission') else f'missing {m}' + (f' ({why})' if why else ''))
    for m in exp.get('must_not', []):
        if m in names(turn): fails.append(f'forbidden {m}')
    seq = exp.get('sequence')
    if seq:
        pos = [called.index(s) if s in called else -1 for s in seq]
        attempted = [c['name'] for c in turn['calls']]
        if all(x in attempted for x in seq) and any(refused.get(x) in ('client_approval', 'server_permission') for x in seq):
            pass  # the model called them in order; the block is already reported above
        elif -1 in pos or pos != sorted(pos): fails.append(f'sequence {seq} not met (got {called})')
    for tool, want in exp.get('args', {}).items():
        got = [c['args'] for c in turn['calls'] if c['name'] == tool]
        if got and not any(all(get(a, k) == v for k, v in want.items()) for a in got):
            fails.append(f'{tool} args {want} not matched (got {got})')
    for tool, req in exp.get('requires_arg', {}).items():
        for c in turn['calls']:
            if c['name'] == tool:
                for r in req:
                    if not c['args'].get(r) and c.get('why') != 'server_permission': fails.append(f'{tool} missing {r}')
    for tool, fields in exp.get('no_invented', {}).items():
        for c in turn['calls']:
            if c['name'] == tool:
                for f in fields:
                    if f in c['args']: fails.append(f'{tool} set {f}={c["args"][f]}, which the brand never said')
    if exp.get('refuse') and any(c['ok'] is not False for c in turn['calls'] if c['name'] in ('update_deal', 'propose_terms_update', 'confirm_terms_update')):
        fails.append('should have refused but wrote')
    return fails

def guards(turn, floor):
    g = []
    user = turn['user'].lower()
    ctx = re.sub(r'[,₹]', '', turn['context'].lower())
    bw = brand_words(turn['context'])
    for c in turn['calls']:
        if c['name'] in ('save_deal', 'update_deal'):
            a = dict(c['args']); a.update(a.pop('clarified', {}) or {})
            for f in ('revisions', 'exclusivityDays', 'fee'):
                v = a.get(f)
                if v is None: continue
                if not re.search(rf'\b{v}\b', ctx):
                    g.append(f'invented: {c["name"]}.{f}={v} appears nowhere in what the user said')
                elif not re.search(rf'\b{v}\b', bw):
                    g.append(f'misattributed: {c["name"]}.{f}={v} is the creator\'s own term, not the brand\'s')
            if a.get('usage') == 'paid_90' and '90' not in ctx:
                m = re.search(r'paid ads for (\d+) days', ctx)
                g.append(f'enum_gap: {c["name"]}.usage=paid_90, brand asked {m.group(1)} days and the enum has nothing nearer' if m
                         else f'invented: {c["name"]}.usage=paid_90, user never said 90')
    if floor and re.search(rf'₹?\s?{floor:,}|₹?\s?{floor}\b', turn['reply']):
        g.append('leak: private minimum in reply')
    n = [c['name'] for c in turn['calls'] if c['ok'] is not False]
    if 'propose_terms_update' in n and 'confirm_terms_update' in n:
        g.append('self_confirm: proposed and confirmed terms in one turn')
    for c in turn['calls']:
        if c['name'] == 'update_deal' and c['args'].get('stage') == 'agreed' and not re.search(r'email|in writing|wrote|written|signed', user):
            g.append('agreed_unproven: no written-confirmation cue in user turn')
    return g

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', required=True, help='JSON: {"<turn file basename>": "<case id>" or {inline expect}}')
    ap.add_argument('--cases', default=__file__.rsplit('/', 1)[0] + '/cases.json')
    ap.add_argument('--floor', type=int, default=0, help='private minimum, for the leak guard')
    ap.add_argument('files', nargs='+')
    o = ap.parse_args()
    cases = {c['id']: c['expect'] for c in json.load(open(o.cases))}
    mp = json.load(open(o.map))
    total = passed = blocked = 0; guard_hits = 0
    by_case = {}
    for f in o.files:
        turn = load_turn(f)
        key = "/".join(f.split("/")[-3:])
        exp = mp.get(key)
        exp = cases.get(exp) if isinstance(exp, str) else exp
        fails = check(turn, exp, o.floor) if exp else []
        g = guards(turn, o.floor)
        model_fails = [x for x in fails if not x.startswith('BLOCKED')]
        total += bool(exp); guard_hits += len(g)
        if exp and not fails: status = 'PASS'; passed += 1
        elif exp and not model_fails: status = 'BLKD'; blocked += 1
        else: status = 'FAIL' if exp else 'n/a '
        if isinstance(mp.get(key), str): by_case.setdefault(mp[key], []).append(status)
        print(f'{status} {key:18} calls={[c["name"] + ("" if c["ok"] is not False else "(" + c.get("why", "refused") + ")") for c in turn["calls"]]}')
        for x in fails + g: print('      -', x)
    if any(len(v) > 1 for v in by_case.values()):
        print('\nper case over repeated runs (pass / blocked / fail):')
        for cid in sorted(by_case):
            v = by_case[cid]
            print(f'  {cid}  {v.count("PASS")}/{v.count("BLKD")}/{v.count("FAIL")} of {len(v)}')
    print(f'\n{passed}/{total} passed · {blocked} blocked by setup or client policy · {total - passed - blocked} model failures · {guard_hits} guard hits')
    return 0 if passed + blocked == total and not guard_hits else 1

if __name__ == '__main__':
    sys.exit(main())
