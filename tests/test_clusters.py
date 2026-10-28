import unittest

from learning.discovery_manager import DiscoveryManager
from learning.mining.clusters import find_clusters, reach
from tests.test_knowledge import DAY, cid, ingest, result


G = (("assignment_group", "G"),)
G_SUN = (("assignment_group", "G"), ("weekday", "Sun"))
G_H10 = (("assignment_group", "G"), ("hour", "10"))
G_S2 = (("assignment_group", "G"), ("business_service", "S2"))
H_SUN = (("assignment_group", "H"), ("weekday", "Sun"))
IMPACT = (("impact", "3"), ("hour", "10"))


def entry(name, items, measure="pickup_delay"):
    return (name, measure, [items])


class ClusterFunctionTest(unittest.TestCase):
    def test_reach_is_specializations_on_the_same_measure(self):
        others = [entry("a", G_SUN), entry("b", G_H10), entry("c", H_SUN), entry("d", G_SUN, "hold_time")]
        self.assertEqual(reach(G, "pickup_delay", others), ["a", "b"])

    def test_clusters_only_on_entities(self):
        entries = [entry("a", G_SUN), entry("b", G_H10), entry("c", G_S2), entry("d", H_SUN), entry("e", IMPACT)]
        clusters = find_clusters(entries)
        self.assertEqual(clusters[0]["items"], [("assignment_group", "G")])
        self.assertEqual(clusters[0]["members"], ["a", "b", "c"])
        self.assertFalse([c for c in clusters if ("weekday", "Sun") in c["items"]])   # a hét napja nem entitás


class ManagerClusterTest(unittest.TestCase):
    def setUp(self):
        self.manager = DiscoveryManager()
        ingest(self.manager, [result(G, ["I1", "I2", "I3", "I4"]), result(G_SUN, ["I1", "I2", "I3"]),
                              result(G_H10, ["I2", "I3", "I4"]), result(H_SUN, ["J1", "J2", "J3"])], DAY)

    def test_general_candidate_is_ranked_first_by_reach(self):
        general = self.manager.candidate(cid(G))
        self.assertEqual(sorted(general.reach), sorted([cid(G_SUN), cid(G_H10)]))
        self.assertEqual(self.manager.queue()[0].candidate_id, cid(G))

    def test_cluster_points_to_the_general_candidate(self):
        [cluster] = [c for c in self.manager.clusters() if c["items"] == [("assignment_group", "G")]]
        self.assertEqual(len(cluster["members"]), 3)
        self.assertEqual(cluster["candidate_id"], cid(G))



class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.manager = DiscoveryManager()
        self.manager.training_capacity, self.manager.decision_capacity, self.manager.training_decisions = 3, 2, 4
        ingest(self.manager, [result(G, ["I1", "I2", "I3", "I4"]), result(G_SUN, ["I1", "I2", "I3"]),
                              result(G_H10, ["I2", "I3", "I4"]), result(H_SUN, ["J1", "J2", "J3"]),
                              result((("assignment_group", "K"),), ["K1", "K2", "K3"])], DAY)

    def test_training_then_steady_each_candidate_counts_once(self):
        cap = self.manager.capacity()
        self.assertEqual((cap["phase"], cap["capacity"], cap["remaining"]), ("training", 3, 3))
        for items in (G, G_SUN, G_H10):
            self.manager.decide(cid(items), "not_finding", "this_occurrence")
        cap = self.manager.capacity()
        self.assertEqual((cap["used_today"], cap["learning_decisions"], cap["phase"]), (3, 3, "training"))
        self.manager.decide(cid(H_SUN), "not_finding", "this_occurrence")
        cap = self.manager.capacity()
        self.assertEqual((cap["phase"], cap["capacity"], cap["used_today"], cap["remaining"]), ("steady", 2, 4, 0))

    def test_queue_is_cut_at_the_remaining_decisions(self):
        # minden jelölt egy döntés (a családokon nincs közös művelet)
        self.assertEqual(len(self.manager.queue(20, decisions=3)), 3)
        self.assertEqual(len(self.manager.queue(20, decisions=1)), 1)
        self.assertEqual(self.manager.queue(20, decisions=0), [])


if __name__ == "__main__":
    unittest.main()
