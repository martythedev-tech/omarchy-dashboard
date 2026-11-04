"""Static checks that Panel.qml keeps Omarchy's plugin rules (Omarchy 4.5 plugin guide):
plain text for every Text, and motion through Style.duration() without ease-in or bounce."""
import re
import unittest
from pathlib import Path

PANEL = (Path(__file__).resolve().parent.parent / 'Panel.qml').read_text()


def blocks(kind):
    """Yield (line number, body) for every `<kind> {` block, braces balanced."""
    lines = PANEL.split('\n')
    for i, line in enumerate(lines):
        if not re.match(r'^\s*(component \w+:\s*)?' + kind + r'\s*\{', line):
            continue
        depth, body = 0, []
        for l in lines[i:]:
            body.append(l)
            depth += l.count('{') - l.count('}')
            if depth <= 0:
                break
        yield i + 1, '\n'.join(body)


class PanelRulesTests(unittest.TestCase):
    def test_every_text_sets_a_text_format(self):
        missing = [n for n, body in blocks('Text') if 'textFormat:' not in body]
        self.assertEqual(missing, [], 'Text without textFormat (AutoText renders markup) at lines %s' % missing)

    def test_rich_text_only_for_the_escaped_diff(self):
        rich = [l.strip() for l in PANEL.split('\n') if 'Text.RichText' in l or 'Text.StyledText' in l]
        self.assertEqual(rich, ['textFormat: root.detailMode === "diff" ? Text.RichText : Text.PlainText'])

    def test_durations_go_through_style(self):
        bare = [l.strip() for l in PANEL.split('\n') if re.search(r'\bduration:\s*\d', l)]
        self.assertEqual(bare, [])

    def test_no_ease_in_bounce_or_overshoot(self):
        bad = re.findall(r'Easing\.(In\w*|OutBounce|OutBack|OutElastic|InOut\w*)', PANEL)
        self.assertEqual(bad, [])

    def test_looping_animations_stop_when_motion_is_reduced(self):
        loops = [n for n, body in blocks(r'SequentialAnimation(\s+on\s+\w+)?')
                 if 'Animation.Infinite' in body and '!Style.reduceMotion' not in body]
        self.assertEqual(loops, [])


if __name__ == '__main__':
    unittest.main()
