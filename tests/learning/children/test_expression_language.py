"""Tesztek: learning/children/expression_language.py."""
import unittest

from learning.children.expression_language import compile_expression, evaluate, rows_to_expression
from learning.children.unit_context import UnitContext
from tests.support.child_fixtures import IMMEDIATE, quick_close, stint_case


class LanguageTest(unittest.TestCase):
    def test_rejects_unknown_and_unsafe(self):
        for bad in ('__import__("os")', "open('x')", "x.y", "lambda: 1", "unknown_var > 1", "ertek(1)[0]"):
            with self.assertRaises(ValueError, msg=bad):
                compile_expression(bad)

    def test_rows_compile_to_same_meaning(self):
        expression = rows_to_expression([{"var": "szakasz_perc", "op": "<", "value": 15}, {"var": "lezarta"},
                                         {"fn": "kitoltve", "arg": "close_notes"}])
        timeline = quick_close("I1", 10)
        unit = UnitContext(timeline, timeline.stints[1], {})
        self.assertTrue(evaluate(compile_expression(expression), unit))
        self.assertEqual(evaluate(compile_expression(expression), unit), evaluate(compile_expression(IMMEDIATE), unit))

    def test_stint_vocabulary(self):
        timeline = quick_close("I1", 10)
        g = UnitContext(timeline, timeline.stints[1], {"L1": "L1"})
        self.assertEqual(g.timeline.stints[1].group, "G")
        check = lambda text: evaluate(compile_expression(text), g)
        self.assertTrue(check(IMMEDIATE))
        self.assertTrue(check('felvetel_perc == 1 and csoport == "G" and not tovabbitotta'))
        self.assertTrue(check('ertek("state") in ["6", "7"] and nyito_csoport == "L1" and zaro_csoport == "G"'))
        self.assertFalse(check('kitoltve("work_notes")'))
        self.assertTrue(check('valtozasok("close_notes") == 1 and csoportok_szama == 2 and not nyit_zar_egyedul'))
        slow = quick_close("I2", 120)
        self.assertFalse(evaluate(compile_expression(IMMEDIATE), UnitContext(slow, slow.stints[1], {})))
        # az L1 szakasza továbbítással ért véget, nem zárta le
        self.assertTrue(evaluate(compile_expression("tovabbitotta and kovetkezo_csoport == 'G'"),
                                 UnitContext(timeline, timeline.stints[0], {})))

    def test_missing_value_is_not_comparable(self):
        timeline = quick_close("I1", 10)
        unit = UnitContext(timeline, timeline.stints[0], {})   # az L1 szakaszában nem lett felelős (üres érték)
        self.assertFalse(evaluate(compile_expression("felvetel_perc > 1000"), unit))

class FriendlySyntaxTest(unittest.TestCase):
    def test_bare_field_names_in_functions(self):
        timeline = quick_close("I1", 10)
        unit = UnitContext(timeline, None, {})   # ticket egység
        self.assertTrue(evaluate(compile_expression("jegy_lezarva and valtozasok(assignment_group) == 2"), unit))
        self.assertTrue(evaluate(compile_expression('kitoltve(close_notes) and ertek(state) == "6"'), unit))
        # a szókincs változói továbbra is változók maradnak
        self.assertTrue(evaluate(compile_expression("szam(csoportok_szama) == 2"), unit))

class InteractionVocabularyTest(unittest.TestCase):
    def check(self, unit, text):
        return evaluate(compile_expression(text), unit)

    def test_first_step_after_pickup(self):
        passed = stint_case([(12, "assignment_group", "G", "H"), (12, "u_routing_instructions", "", "R")])
        self.assertTrue(self.check(passed, 'elso_lepes_felvetel_utan == "tovabbitas" and interakciok_felvetel_utan == 0'))
        worked = stint_case([(20, "work_notes", "", "megnéztem"), (40, "close_notes", "", "kész"), (40, "state", "1", "6")])
        self.assertTrue(self.check(worked, 'elso_lepes_felvetel_utan == "interakcio" and interakciok == 1 and lezarta'))
        closed = stint_case([(13, "close_notes", "", "kész"), (13, "work_notes", "", "zárom"), (13, "state", "1", "7")])
        self.assertTrue(self.check(closed, 'elso_lepes_felvetel_utan == "lezaras" and interakciok == 0 and szakasz_perc <= 30'))
        held = stint_case([(15, "state", "1", "3"), (16, "u_pending_reason", "", "100")])
        self.assertTrue(self.check(held, 'elso_lepes_felvetel_utan == "allapot" and interakciok_felvetel_utan == 1'))
        idle = stint_case([])
        self.assertTrue(self.check(idle, 'elso_lepes_felvetel_utan == "nincs" and felvetel_utan_perc > 0'))

class InProgressIsNotAStepTest(unittest.TestCase):
    def test_in_progress_is_skipped(self):
        unit = stint_case([(10, "state", "1", "2"), (12, "assignment_group", "G", "H")])
        self.assertTrue(evaluate(compile_expression('elso_lepes_felvetel_utan == "tovabbitas"'), unit))

class TimeVocabularyTest(unittest.TestCase):
    """START = 2026-08-03 hétfő 9:00; L1 -> G az 5. percben, névre vétel a 6. percben."""

    def test_previous_group_and_moments(self):
        timeline = quick_close("I1", 10)
        g = UnitContext(timeline, timeline.stints[1], {"L1": "L1"})
        check = lambda text: evaluate(compile_expression(text), g)
        self.assertTrue(check('elozo_csoport == "L1" and elozo_szint == "L1" and nyito_szint == "L1"'))
        self.assertTrue(check('nyitas_nap == "hétfő" and nyitas_ora == 9 and not nyitas_hetvege'))
        self.assertTrue(check('kapta_munkaidoben and felvette_munkaidoben and vege_ora == 9'))
        self.assertTrue(check("felvetel_munkaperc == 1 and not munkarend_ismert"))
        first = UnitContext(timeline, timeline.stints[0], {"L1": "L1"})
        self.assertEqual(evaluate(compile_expression('elozo_csoport == ""'), first), True)

    def test_group_schedule_decides_working_time(self):
        from model.working_time.schedule import Schedule
        timeline = quick_close("I1", 10)
        night = {"G": Schedule(frozenset(range(7)), 20, 6)}   # éjszakás csoport: hétfő 9-kor nincs munkaidő
        g = UnitContext(timeline, timeline.stints[1], {}, night)
        check = lambda text: evaluate(compile_expression(text), g)
        self.assertTrue(check("munkarend_ismert and kapta_munkaidoben == False and felvetel_munkaperc == 0"))


if __name__ == "__main__":
    unittest.main()
