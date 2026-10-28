"""Tesztek: learning/mining/clusters.py."""
import unittest

from learning.mining.clusters import find_clusters, reach
from tests.support.cluster_fixtures import G, G_H10, G_S2, G_SUN, H_SUN, IMPACT, entry


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


if __name__ == "__main__":
    unittest.main()
