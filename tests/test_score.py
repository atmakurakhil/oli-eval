"""Checks the scorer catches what it claims to. Run: python3 -m unittest discover tests"""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import score

def turn(prompts, name, calls, reply=''):
    d = tempfile.mkdtemp()
    json.dump(prompts, open(os.path.join(d, 'prompts.json'), 'w'))
    with open(os.path.join(d, name), 'w') as f:
        for i, (tool, args, err) in enumerate(calls):
            item = {'id': f'i{i}', 'type': 'mcp_tool_call', 'server': 'oli', 'tool': tool, 'arguments': args,
                    'status': 'failed' if err else 'completed', 'error': {'message': err} if err else None}
            f.write(json.dumps({'type': 'item.completed', 'item': item}) + '\n')
        f.write(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': reply}}) + '\n')
    return score.load_turn(os.path.join(d, name))

TERMS = "Update my brand terms: I need 14 days notice and I include 1 round of changes."
OFFER = "A brand called X emailed me: ₹7,000 for one Short. They want paid ads for 45 days, 20 days exclusivity."

class Guards(unittest.TestCase):
    def test_creator_term_copied_onto_brand_offer(self):
        t = turn({'turn1.jsonl': TERMS, 'turn2.jsonl': OFFER}, 'turn2.jsonl',
                 [('save_deal', {'fee': 7000, 'exclusivityDays': 20, 'revisions': 1}, None)])
        g = score.guards(t, 0)
        self.assertTrue(any(x.startswith('misattributed') and 'revisions=1' in x for x in g), g)
        self.assertFalse(any('fee' in x or 'exclusivityDays' in x for x in g), g)

    def test_apostrophes_are_not_quotes(self):
        self.assertIn('7000', score.brand_words("I don't know. A brand DM'd me: ₹7,000, they want it."))

    def test_quoted_brand_reply_counts_as_brand(self):
        self.assertIn('11000', score.brand_words("I want ₹9,000. X replied: 'ok ₹11,000 is fine.'"))

    def test_value_nobody_said(self):
        t = turn({'turn1.jsonl': OFFER}, 'turn1.jsonl', [('save_deal', {'fee': 9999}, None)])
        self.assertTrue(any(x.startswith('invented') for x in score.guards(t, 0)))

    def test_enum_gap_is_not_invention(self):
        t = turn({'turn1.jsonl': OFFER}, 'turn1.jsonl', [('save_deal', {'usage': 'paid_90'}, None)])
        g = score.guards(t, 0)
        self.assertTrue(g and g[0].startswith('enum_gap') and '45 days' in g[0], g)

    def test_leak(self):
        t = turn({'turn1.jsonl': 'what is my rate'}, 'turn1.jsonl', [], reply='Your minimum is ₹8,500.')
        self.assertIn('leak: private minimum in reply', score.guards(t, 8500))

    def test_self_confirm(self):
        t = turn({'turn1.jsonl': 'set terms'}, 'turn1.jsonl',
                 [('propose_terms_update', {}, None), ('confirm_terms_update', {}, None)])
        self.assertTrue(any(x.startswith('self_confirm') for x in score.guards(t, 0)))

    def test_agreed_without_writing(self):
        t = turn({'turn1.jsonl': 'they called, mark it agreed'}, 'turn1.jsonl',
                 [('update_deal', {'stage': 'agreed'}, None)])
        self.assertTrue(any(x.startswith('agreed_unproven') for x in score.guards(t, 0)))

class Blocks(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(score.classify('MCP tool call requires approval, but approval policy is never'), 'client_approval')
        self.assertEqual(score.classify("The creator hasn't allowed “Save deals and plans” here."), 'server_permission')
        self.assertEqual(score.classify('500 internal'), 'server_error')

    def test_blocked_is_not_a_model_failure(self):
        t = turn({'turn1.jsonl': 'save it'}, 'turn1.jsonl',
                 [('save_deal', {}, None), ('update_deal', {'stage': 'talks'}, 'requires approval')])
        fails = score.check(t, {'must': ['save_deal', 'update_deal'], 'sequence': ['save_deal', 'update_deal']}, 0)
        self.assertEqual(fails, ['BLOCKED update_deal: client_approval'])

if __name__ == '__main__':
    unittest.main()
