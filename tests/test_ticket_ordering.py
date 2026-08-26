import pathlib
import unittest

from council_tools.ticket_contracts import MAX_LIST_ITEMS

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
from council_tools.ticket_ordering import TicketOrderingError, order_children


class OrderTest(unittest.TestCase):
    def test_a_chain_is_ordered_by_its_relations(self):
        self.assertEqual(
            order_children({"c": ["b"], "b": ["a"], "a": []}), ("a", "b", "c")
        )

    def test_a_diamond_places_both_middles_after_the_root(self):
        order = order_children({"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []})
        self.assertEqual(order, ("a", "b", "c", "d"))

    def test_a_child_that_depends_on_nothing_is_included(self):
        self.assertEqual(order_children({"only": []}), ("only",))

    def test_independent_children_are_all_returned(self):
        self.assertEqual(order_children({"b": [], "a": [], "c": []}), ("a", "b", "c"))

    def test_every_child_follows_every_sibling_it_depends_on(self):
        graph = {"e": ["c"], "c": ["a", "b"], "d": ["b"], "a": [], "b": ["a"]}
        order = order_children(graph)
        position = {key: index for index, key in enumerate(order)}
        self.assertEqual(sorted(order), sorted(graph))
        for key, deps in graph.items():
            for dep in deps:
                with self.subTest(child=key, dependency=dep):
                    self.assertLess(position[dep], position[key])


class ReproducibilityTest(unittest.TestCase):
    """The order must be a function of the relations, not of the input sequence."""

    def test_input_sequence_does_not_change_the_order(self):
        pairs = [("c", ["a", "b"]), ("b", ["a"]), ("a", []), ("d", ["b"])]
        expected = order_children(dict(pairs))
        for rotation in range(len(pairs)):
            rotated = dict(pairs[rotation:] + pairs[:rotation])
            with self.subTest(rotation=rotation):
                self.assertEqual(order_children(rotated), expected)

    def test_reversed_input_gives_the_identical_order(self):
        graph = {"a": [], "b": ["a"], "c": ["b"], "d": ["a"]}
        self.assertEqual(
            order_children(graph), order_children(dict(reversed(list(graph.items()))))
        )

    def test_ties_are_broken_lexicographically(self):
        self.assertEqual(order_children({"z": [], "y": [], "x": []}), ("x", "y", "z"))

    def test_two_calls_return_the_same_value(self):
        graph = {"b": ["a"], "a": []}
        self.assertEqual(order_children(graph), order_children(graph))

    def test_the_caller_input_is_not_mutated(self):
        graph = {"b": ["a"], "a": []}
        before = {key: list(value) for key, value in graph.items()}
        order_children(graph)
        self.assertEqual(graph, before)

    def test_the_result_is_an_immutable_tuple(self):
        self.assertIsInstance(order_children({"a": []}), tuple)


class CycleTest(unittest.TestCase):
    def test_two_child_cycle_is_refused_and_names_its_keys(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": ["b"], "b": ["a"]})
        self.assertEqual(caught.exception.code, "dependency-cycle")
        self.assertIn("a", caught.exception.detail)
        self.assertIn("b", caught.exception.detail)

    def test_three_child_cycle_is_refused(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": ["c"], "b": ["a"], "c": ["b"]})
        self.assertEqual(caught.exception.code, "dependency-cycle")
        for key in ("a", "b", "c"):
            self.assertIn(key, caught.exception.detail)

    def test_the_cycle_message_is_reproducible(self):
        graph = {"a": ["c"], "b": ["a"], "c": ["b"]}
        details = set()
        for _ in range(3):
            with self.assertRaises(TicketOrderingError) as caught:
                order_children(graph)
            details.add(caught.exception.detail)
        self.assertEqual(len(details), 1)

    def test_the_cycle_message_does_not_depend_on_input_sequence(self):
        """A dict iterates in insertion order, so a walk that starts wherever it
        lands names a different cycle for the same graph."""
        pairs = [("a", ["c"]), ("b", ["a"]), ("c", ["b"]), ("d", ["a"])]
        details = set()
        for rotation in range(len(pairs)):
            rotated = dict(pairs[rotation:] + pairs[:rotation])
            with self.assertRaises(TicketOrderingError) as caught:
                order_children(rotated)
            details.add(caught.exception.detail)
        self.assertEqual(len(details), 1, f"cycle message varied: {details}")

    def test_the_cycle_message_does_not_depend_on_the_hash_seed(self):
        """Edges are held in a set, whose iteration order is stable within one
        process and varies across processes because str hashing is randomized.
        A walk over an unsorted set is reproducible only until it runs somewhere
        else, which is exactly when a plan gets compared."""
        import os
        import subprocess
        import sys

        program = (
            "import sys;"
            "sys.path.insert(0, 'src');"
            "from council_tools.ticket_ordering import order_children,"
            " TicketOrderingError\n"
            "try:\n"
            "    order_children({'a': ['b', 'c'], 'b': ['a'], 'c': ['a'],"
            " 'd': ['a']})\n"
            "except TicketOrderingError as exc:\n"
            "    print(exc.detail)\n"
        )
        details = set()
        for seed in ("0", "1", "7", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH="src")
            result = subprocess.run(
                [sys.executable, "-c", program],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            details.add(result.stdout.strip())
        self.assertEqual(len(details), 1, f"cycle message varied by seed: {details}")

    def test_a_cycle_beside_orderable_children_is_still_refused(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": [], "b": ["c"], "c": ["b"]})
        self.assertEqual(caught.exception.code, "dependency-cycle")

    def test_self_dependency_is_a_distinct_code(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": ["a"]})
        self.assertEqual(caught.exception.code, "self-dependency")


class RefusalTest(unittest.TestCase):
    def test_unknown_key_is_refused_and_names_both_sides(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": ["ghost"]})
        self.assertEqual(caught.exception.code, "unknown-key")
        self.assertEqual(caught.exception.detail, "ghost")
        self.assertIn("a", caught.exception.field)

    def test_empty_set_is_refused_rather_than_returning_an_empty_order(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({})
        self.assertEqual(caught.exception.code, "empty-children")

    def test_a_set_over_the_contract_list_bound_is_refused(self):
        graph = {f"k{index:03d}": [] for index in range(MAX_LIST_ITEMS + 1)}
        with self.assertRaises(TicketOrderingError) as caught:
            order_children(graph)
        self.assertEqual(caught.exception.code, "too-many-children")

    def test_a_set_exactly_at_the_bound_is_accepted(self):
        graph = {f"k{index:03d}": [] for index in range(MAX_LIST_ITEMS)}
        self.assertEqual(len(order_children(graph)), MAX_LIST_ITEMS)

    def test_non_mapping_children_are_refused(self):
        for value in ([("a", [])], None, "a", 1):
            with self.subTest(value=value):
                with self.assertRaises(TicketOrderingError) as caught:
                    order_children(value)
                self.assertEqual(caught.exception.code, "invalid-children")

    def test_non_text_key_is_refused(self):
        for key in (1, None, ("a",)):
            with self.subTest(key=key):
                with self.assertRaises(TicketOrderingError) as caught:
                    order_children({key: []})
                self.assertEqual(caught.exception.code, "invalid-key")

    def test_whitespace_padded_and_empty_keys_are_refused_not_normalized(self):
        for key in ("", " a", "a ", "a\n", " "):
            with self.subTest(key=key):
                with self.assertRaises(TicketOrderingError) as caught:
                    order_children({key: []})
                self.assertEqual(caught.exception.code, "non-canonical-key")

    def test_a_padded_dependency_is_refused_rather_than_matched(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": [], "b": ["a "]})
        self.assertEqual(caught.exception.code, "non-canonical-key")

    def test_non_iterable_dependencies_are_refused(self):
        for deps in (None, 1, object()):
            with self.subTest(deps=deps):
                with self.assertRaises(TicketOrderingError) as caught:
                    order_children({"a": deps})
                self.assertEqual(caught.exception.code, "invalid-dependencies")

    def test_a_text_dependency_list_is_refused_rather_than_iterated_by_character(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": [], "ab": "a"})
        self.assertEqual(caught.exception.code, "invalid-dependencies")

    def test_duplicate_dependency_is_refused(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children({"a": [], "b": ["a", "a"]})
        self.assertEqual(caught.exception.code, "duplicate-dependency")

    def test_error_is_a_value_error_with_a_stable_code_and_field(self):
        with self.assertRaises(ValueError) as caught:
            order_children({"a": ["ghost"]})
        self.assertEqual(caught.exception.code, "unknown-key")
        self.assertEqual(caught.exception.field, "children[a]")


class DuplicateKeyTest(unittest.TestCase):
    """A dict cannot yield a key twice; a custom Mapping can."""

    class Doubled(dict):
        def items(self):
            return [("a", []), ("a", [])]

    def test_a_mapping_yielding_one_key_twice_is_refused(self):
        with self.assertRaises(TicketOrderingError) as caught:
            order_children(self.Doubled())
        self.assertEqual(caught.exception.code, "duplicate-key")


if __name__ == "__main__":
    unittest.main()
