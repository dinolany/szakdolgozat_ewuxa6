"""Közös tesztadatok és -segédek.

Használja: learning/mining/test_clusters.py, learning/test_discovery_manager.py."""
G = (("assignment_group", "G"),)

G_SUN = (("assignment_group", "G"), ("weekday", "Sun"))

G_H10 = (("assignment_group", "G"), ("hour", "10"))

G_S2 = (("assignment_group", "G"), ("business_service", "S2"))

H_SUN = (("assignment_group", "H"), ("weekday", "Sun"))

IMPACT = (("impact", "3"), ("hour", "10"))

def entry(name, items, measure="pickup_delay"):
    return (name, measure, [items])
