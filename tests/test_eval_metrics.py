"""O9/D3：评估指标纯函数测试（离线、无外部依赖）。"""
from eval import metrics


def test_ndcg_perfect_when_relevant_in_order():
    # 相关项连续排在顶部（即理想排序）-> NDCG == 1.0
    ranked = ["a", "c", "b", "d"]
    relevant = {"a", "c"}
    assert metrics.ndcg_at_k(ranked, relevant, 4) == 1.0


def test_ndcg_lower_when_relevant_ranked_lower():
    ranked = ["b", "a", "c", "d"]  # a 从第 1 降到第 2 位
    relevant = {"a", "c"}
    val = metrics.ndcg_at_k(ranked, relevant, 4)
    assert 0.0 < val < 1.0


def test_ndcg_zero_when_no_relevant():
    assert metrics.ndcg_at_k(["a", "b"], {"z"}, 4) == 0.0


def test_hit_rate():
    assert metrics.hit_rate_at_k(["a", "b"], {"c"}, 2) == 0.0
    assert metrics.hit_rate_at_k(["a", "b"], {"b"}, 2) == 1.0
    assert metrics.hit_rate_at_k(["a"], {"x"}, 5) == 0.0  # 无相关集 -> 0


def test_recall_at_k():
    # 相关 {c,d}，top4 仅召回 c -> 0.5
    assert metrics.recall_at_k(["c", "x", "y"], {"c", "d"}, 4) == 0.5
    assert metrics.recall_at_k(["c"], {"c", "d"}, 1) == 0.5
    assert metrics.recall_at_k(["x"], {"c", "d"}, 4) == 0.0


def test_diversity_identical_genres_is_zero():
    items = [{"genres": ["动作", "科幻"]}, {"genres": ["动作", "科幻"]}]
    assert metrics.diversity_at_k(items, 2) == 0.0


def test_diversity_disjoint_genres_is_one():
    items = [{"genres": ["动作"]}, {"genres": ["科幻"]}]
    assert metrics.diversity_at_k(items, 2) == 1.0


def test_diversity_single_or_empty():
    # P2-22：无 pairwise 比较 / 全空类型 → 返回 None（"无信息"），不得当作 1.0 计入均值
    assert metrics.diversity_at_k([{"genres": ["动作"]}], 2) is None
    assert metrics.diversity_at_k([], 2) is None
    assert metrics.diversity_at_k([{"genres": []}, {"genres": []}], 2) is None
    # 有类型信息且不全相同 → 正常 [0,1]
    assert metrics.diversity_at_k([{"genres": ["动作"]}, {"genres": ["科幻"]}], 2) == 1.0


def test_diversity_accepts_candidate_like():
    class C:
        def __init__(self, g):
            self.genres = g

    items = [C(["动作"]), C(["科幻"])]
    assert metrics.diversity_at_k(items, 2) == 1.0


def test_mrr_at_k():
    # 首位相关 → 1.0；第 3 位 → 1/3；无命中 → 0
    assert metrics.mrr_at_k(["a", "b"], {"a"}, 10) == 1.0
    assert abs(metrics.mrr_at_k(["x", "y", "a"], {"a"}, 10) - 1 / 3) < 1e-9
    assert metrics.mrr_at_k(["x", "y"], {"a"}, 10) == 0.0
    assert metrics.mrr_at_k(["a"], set(), 10) == 0.0


def test_map_at_k():
    # 相关 {a,b}：命中位置 1,3 → AP = (1/1 + 2/3)/2
    ap = (1.0 + 2 / 3) / 2
    assert abs(metrics.map_at_k(["a", "x", "b"], {"a", "b"}, 10) - ap) < 1e-9
    # 仅在 k 内命中才算
    assert metrics.map_at_k(["x", "a"], {"a", "b"}, 1) == 0.0  # b 未在 top1
    assert metrics.map_at_k(["a"], set(), 10) == 0.0


def test_first_relevant_position():
    assert metrics.first_relevant_position(["x", "y", "a"], {"a"}, 10) == 3
    assert metrics.first_relevant_position(["a", "b"], {"a"}, 10) == 1
    assert metrics.first_relevant_position(["x", "y"], {"a"}, 10) == 0  # 无命中
    assert metrics.first_relevant_position(["x", "a"], {"a"}, 2) == 2
