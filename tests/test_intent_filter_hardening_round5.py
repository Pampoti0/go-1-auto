import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import entity_resolver
import server


FAKE_ENTITIES = [
    {
        "id": "url:product",
        "label": "product",
        "scope": "url",
        "type": "url_group",
        "aliases": ["product", "san pham", "sản phẩm", "product page", "trang sản phẩm"],
        "patterns": ["product"],
    },
    {
        "id": "url:solutions",
        "label": "solutions",
        "scope": "url",
        "type": "url_group",
        "aliases": ["solutions", "solution", "giai phap", "giải pháp", "solution page"],
        "patterns": ["solutions"],
    },
    {
        "id": "url:docs",
        "label": "docs",
        "scope": "url",
        "type": "url_group",
        "aliases": ["docs", "documentation", "tai lieu", "tài liệu", "developer docs"],
        "patterns": ["docs"],
    },
    {
        "id": "url:blog",
        "label": "blog",
        "scope": "url",
        "type": "url_group",
        "aliases": ["blog", "article", "bài blog", "bai blog", "knowledge article"],
        "patterns": ["blog"],
    },
    {
        "id": "url:pricing",
        "label": "pricing",
        "scope": "url",
        "type": "url_group",
        "aliases": ["pricing", "price", "bang gia", "bảng giá", "price page"],
        "patterns": ["pricing"],
    },
    {
        "id": "url:case-study",
        "label": "case study",
        "scope": "url",
        "type": "url_group",
        "aliases": ["case study", "case-study", "customer story", "success story"],
        "patterns": ["case-study"],
    },
    {
        "id": "url:cloud-gpu-x",
        "label": "cloud gpu x",
        "scope": "url",
        "type": "url",
        "aliases": [
            "CloudGPUX",
            "Cloud-GPU-X",
            "cloud gpu x",
            "cloudgpux",
            "product cloud gpu x",
            "product/cloud-gpu-x",
        ],
        "patterns": ["product/cloud-gpu-x"],
    },
    {
        "id": "url:gpuaas",
        "label": "gpuaas",
        "scope": "url",
        "type": "url",
        "aliases": [
            "GPUaaS",
            "gpu aas",
            "gpu-as-a-service",
            "GPU as a Service",
            "product/gpuaas",
        ],
        "patterns": ["product/gpuaas"],
    },
    {
        "id": "url:mlops-control-plane",
        "label": "mlops control plane",
        "scope": "url",
        "type": "url",
        "aliases": [
            "MLOps",
            "mlops",
            "MLOpsControlPlane",
            "mlops control plane",
            "solutions/mlops-control-plane",
        ],
        "patterns": ["solutions/mlops-control-plane"],
    },
    {
        "id": "url:k8s-h200-gpu",
        "label": "k8s h200 gpu",
        "scope": "url",
        "type": "url",
        "aliases": [
            "K8s",
            "k8s h200",
            "H200",
            "K8sH200",
            "K8s H200 GPU",
            "docs/k8s-h200-gpu",
        ],
        "patterns": ["docs/k8s-h200-gpu"],
    },
    {
        "id": "url:h100-readers",
        "label": "h100 readers",
        "scope": "url",
        "type": "url",
        "aliases": ["H100Readers", "h100 readers", "h100readers", "blog/h100-readers"],
        "patterns": ["blog/h100-readers"],
    },
    {
        "id": "url:cloud-gpu-x-pricing",
        "label": "cloud gpu x pricing",
        "scope": "url",
        "type": "url",
        "aliases": ["CloudGPUX pricing", "pricing cloud gpu x", "pricing/cloud-gpu-x"],
        "patterns": ["pricing/cloud-gpu-x"],
    },
    {
        "id": "url:brandlift-q3",
        "label": "brand lift q3",
        "scope": "url",
        "type": "url",
        "aliases": [
            "BrandLiftQ3",
            "brand lift q3",
            "case study brand lift q3",
            "case-study/brandlift-q3",
        ],
        "patterns": ["case-study/brandlift-q3"],
    },
    {
        "id": "url:abm-cloud-gpu",
        "label": "abm cloud gpu",
        "scope": "url",
        "type": "url",
        "aliases": ["ABMCloudGPU", "abm cloud gpu", "solutions/abm-cloud-gpu"],
        "patterns": ["solutions/abm-cloud-gpu"],
    },
    {
        "id": "url:h200-vs-h100",
        "label": "h200 vs h100",
        "scope": "url",
        "type": "url",
        "aliases": ["H200 vs H100", "h200-vs-h100", "blog/h200-vs-h100"],
        "patterns": ["blog/h200-vs-h100"],
    },
    {
        "id": "campaign:brand",
        "label": "brand campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["brand", "brand campaign", "decho", "decho brand", "brand lift"],
        "patterns": ["decho", "brandlift"],
    },
    {
        "id": "campaign:competitor",
        "label": "competitor campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["competitor", "doi thu", "đối thủ", "aws", "azure", "gcp"],
        "patterns": ["aws", "azure", "gcp"],
    },
    {
        "id": "campaign:abm",
        "label": "abm campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["abm", "account based marketing", "abm campaign"],
        "patterns": ["abm"],
    },
    {
        "id": "campaign:cloud-gpu-x-search",
        "label": "GG_Search_Cloud_GPU_X",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "GG_Search_Cloud_GPU_X",
            "gg search cloud gpu x",
            "gg_search_cloud_gpu_x",
            "CloudGPUX",
            "Cloud-GPU-X",
            "cloudgpux",
        ],
        "patterns": ["gg-search-cloud-gpu-x"],
    },
    {
        "id": "campaign:pmax-gpuaas-q2",
        "label": "PMAX_GPUaaS_Q2",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["PMAX_GPUaaS_Q2", "pmax gpuaas q2", "GPUaaS", "gpu aas"],
        "patterns": ["pmax-gpuaas-q2"],
    },
    {
        "id": "campaign:mlops-k8s-h200",
        "label": "MLOps_K8s_H200",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["MLOps_K8s_H200", "MLOpsK8sH200", "MLOps", "K8s H200", "H200"],
        "patterns": ["mlops-k8s-h200"],
    },
    {
        "id": "campaign:remarketing-h100-readers",
        "label": "Remarketing_H100Readers",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["Remarketing_H100Readers", "H100Readers", "h100 readers", "h100readers"],
        "patterns": ["remarketing-h100-readers"],
    },
    {
        "id": "campaign:brandlift-q3",
        "label": "BrandLiftQ3",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["BrandLiftQ3", "Brand_Lift_Q3", "brand lift q3"],
        "patterns": ["brandlift-q3"],
    },
    {
        "id": "campaign:abm-cloud-gpu",
        "label": "ABMCloudGPU",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["ABMCloudGPU", "ABM_Cloud_GPU", "abm cloud gpu"],
        "patterns": ["abm-cloud-gpu"],
    },
    {
        "id": "campaign:cloud-gpu-x-alliance",
        "label": "Cloud-GPU-X-Alliance",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["Cloud-GPU-X-Alliance", "Cloud GPU X Alliance", "alliance cloud gpu x"],
        "patterns": ["cloud-gpu-x-alliance"],
    },
]


def fake_catalog(urls=None):
    return deepcopy(FAKE_ENTITIES)


URL_COLLISIONS = [
    ("CloudGPUX", "product/cloud-gpu-x"),
    ("Cloud-GPU-X", "product/cloud-gpu-x"),
    ("GPUaaS", "product/gpuaas"),
    ("MLOps", "solutions/mlops-control-plane"),
    ("K8s H200", "docs/k8s-h200-gpu"),
    ("H200", "docs/k8s-h200-gpu"),
    ("H100Readers", "blog/h100-readers"),
    ("BrandLiftQ3", "case-study/brandlift-q3"),
    ("ABMCloudGPU", "solutions/abm-cloud-gpu"),
]

CAMPAIGN_COLLISIONS = [
    ("CloudGPUX", "gg-search-cloud-gpu-x"),
    ("Cloud-GPU-X", "gg-search-cloud-gpu-x"),
    ("GPUaaS", "pmax-gpuaas-q2"),
    ("MLOps", "mlops-k8s-h200"),
    ("K8s H200", "mlops-k8s-h200"),
    ("H200", "mlops-k8s-h200"),
    ("H100Readers", "remarketing-h100-readers"),
    ("BrandLiftQ3", "brandlift-q3"),
    ("ABMCloudGPU", "abm-cloud-gpu"),
]

URL_LONG_TEMPLATES = [
    "GPUaaS là gì và có nên mua hay thuê, nhưng phần cuối hãy xem traffic {name} trong 14 ngày",
    "trước hết giải thích MLOps khác K8s ra sao; sau đó báo cáo conversion của {name}",
    "tôi hỏi kiến thức về H200 vs H100 ở đầu câu, còn metric thì cần LCP mobile cho {name}",
    "nên chọn cloud hay on-prem là câu hỏi nền, nhưng hãy phân tích ranking keyword của {name}",
    "xem CTR organic của {name}; đoạn sau chỉ là hỏi BrandLiftQ3 có nghĩa gì",
    "báo cáo PageSpeed {name} hôm nay, rồi giải thích GPUaaS cho sale ở phần ghi chú",
]

CAMPAIGN_LONG_TEMPLATES = [
    "brand lift là gì trong Ads, nhưng nửa sau hãy xem spend của campaign {name}",
    "trước hết hỏi ABM có nghĩa gì; sau đó phân tích CPA campaign {name}",
    "PMAX nên dùng khi nào là câu hỏi nền, còn metric cần CTR của {name}",
    "xem reach và frequency campaign {name}; phần cuối chỉ hỏi MLOps là gì",
    "báo cáo conversion value {name}, rồi giải thích H100Readers là audience gì",
    "auction insights là gì không quan trọng, cần cost của campaign {name} trong tuần này",
]

LONG_MIXED_CLAUSE_CASES = [
    (template.format(name=name), "url", [pattern], [], "any")
    for name, pattern in URL_COLLISIONS[:8]
    for template in URL_LONG_TEMPLATES[:3]
] + [
    (template.format(name=name), "campaign", [pattern], [], "any")
    for name, pattern in CAMPAIGN_COLLISIONS[:8]
    for template in CAMPAIGN_LONG_TEMPLATES[:3]
]

PUNCTUATION_AND_SPACING_CASES = [
    ('url:  "{name}"  ; xem traffic'.format(name=name), "url", [pattern], [], "any")
    for name, pattern in URL_COLLISIONS[:6]
] + [
    ("trang ({name}) / score".format(name=name), "url", [pattern], [], "any")
    for name, pattern in URL_COLLISIONS[3:9]
] + [
    ("path /{path}: LCP; CLS".format(path=pattern), "url", [pattern], [], "any")
    for _, pattern in URL_COLLISIONS[:8]
] + [
    ('campaign:  "{name}"  ; CTR'.format(name=name), "campaign", [pattern], [], "any")
    for name, pattern in CAMPAIGN_COLLISIONS[:6]
] + [
    ("ads ({name}) / CPA".format(name=name), "campaign", [pattern], [], "any")
    for name, pattern in CAMPAIGN_COLLISIONS[3:9]
] + [
    ("campaign {name}  ;  spend / leads".format(name=name), "campaign", [pattern], [], "any")
    for name, pattern in CAMPAIGN_COLLISIONS[:8]
]

SCOPE_AMBIGUITY_CASES = [
    ("SEO nói campaign nhưng scope URL: {name} traffic".format(name=name), "url", [pattern], [], "any")
    for name, pattern in URL_COLLISIONS
] + [
    ("Ads nói url nhưng scope campaign: {name} spend".format(name=name), "campaign", [pattern], [], "any")
    for name, pattern in CAMPAIGN_COLLISIONS
] + [
    ("SEO và Ads cùng nhắc, url {name} có CTR organic ra sao".format(name=name), "url", [pattern], [], "any")
    for name, pattern in URL_COLLISIONS[:8]
] + [
    ("SEO và Ads cùng nhắc, campaign {name} có CPA ra sao".format(name=name), "campaign", [pattern], [], "any")
    for name, pattern in CAMPAIGN_COLLISIONS[:8]
] + [
    ("campaign product/cloud-gpu-x spend", "campaign", [], [], "any"),
    ("campaign docs/k8s-h200-gpu CPA", "campaign", [], [], "any"),
    ("campaign pricing/cloud-gpu-x CTR", "campaign", [], [], "any"),
    ("url GG_Search_Cloud_GPU_X traffic", "url", [], [], "any"),
    ("url PMAX_GPUaaS_Q2 ranking", "url", [], [], "any"),
    ("url MLOps_K8s_H200 clicks", "url", [], [], "any"),
    ("SEO cloud-gpu-x-alliance traffic", "url", [], [], "any"),
    ("Ads case-study/brandlift-q3 spend", "campaign", [], [], "any"),
]

NEGATION_SUPPRESSION_CASES = [
    ("không phải {name} traffic, hỏi tổng quan thôi", "seo_query", "url")
    for name, _ in URL_COLLISIONS[:6]
] + [
    ("not {name} conversion, just explain the concept", "seo_query", "url")
    for name, _ in URL_COLLISIONS[3:9]
] + [
    ("đừng lọc {name}, chỉ hỏi định nghĩa", "seo_query", "url")
    for name, _ in URL_COLLISIONS[:6]
] + [
    ("không cần filter {name}, chỉ hỏi kiến thức", "seo_query", "url")
    for name, _ in URL_COLLISIONS[3:9]
] + [
    ("không phải campaign {name}, hỏi Ads naming thôi", "ads_perf", "campaign")
    for name, _ in CAMPAIGN_COLLISIONS[:6]
] + [
    ("not campaign {name}, explain generally", "ads_perf", "campaign")
    for name, _ in CAMPAIGN_COLLISIONS[3:9]
] + [
    ("đừng lọc campaign {name}, hỏi chiến lược thôi", "ads_perf", "campaign")
    for name, _ in CAMPAIGN_COLLISIONS[:6]
]

TRUE_EXCLUSION_CASES = [
    ("CloudGPUX trừ GPUaaS traffic", "url", ["product/cloud-gpu-x"], ["product/gpuaas"], "any"),
    ("GPUaaS except MLOps conversion", "url", ["product/gpuaas"], ["solutions/mlops-control-plane"], "any"),
    ("MLOps exclude K8s H200 ranking", "url", ["solutions/mlops-control-plane"], ["docs/k8s-h200-gpu"], "any"),
    ("K8s H200 không gồm H100Readers score", "url", ["docs/k8s-h200-gpu"], ["blog/h100-readers"], "any"),
    ("H100Readers ngoại trừ BrandLiftQ3 CTR", "url", ["blog/h100-readers"], ["case-study/brandlift-q3"], "any"),
    ("BrandLiftQ3 trừ ABMCloudGPU users", "url", ["case-study/brandlift-q3"], ["solutions/abm-cloud-gpu"], "any"),
    ("ABMCloudGPU except Cloud-GPU-X LCP", "url", ["solutions/abm-cloud-gpu"], ["product/cloud-gpu-x"], "any"),
    ("url phải chứa cả CloudGPUX và GPUaaS trừ pricing/cloud-gpu-x", "url", ["product/cloud-gpu-x", "product/gpuaas"], ["pricing/cloud-gpu-x"], "all"),
    ("CloudGPUX trừ drop table", "url", ["product/cloud-gpu-x"], ["drop-table"], "any"),
    ("GPUaaS không gồm ../private", "url", ["product/gpuaas"], [], "any"),
    ("campaign CloudGPUX trừ GPUaaS spend", "campaign", ["gg-search-cloud-gpu-x"], ["pmax-gpuaas-q2"], "any"),
    ("campaign GPUaaS except MLOps CPA", "campaign", ["pmax-gpuaas-q2"], ["mlops-k8s-h200"], "any"),
    ("campaign MLOps exclude H100Readers CTR", "campaign", ["mlops-k8s-h200"], ["remarketing-h100-readers"], "any"),
    ("campaign H100Readers không gồm BrandLiftQ3 spend", "campaign", ["remarketing-h100-readers"], ["brandlift-q3"], "any"),
    ("campaign BrandLiftQ3 ngoại trừ ABMCloudGPU leads", "campaign", ["brandlift-q3"], ["abm-cloud-gpu"], "any"),
    ("campaign ABMCloudGPU trừ Cloud-GPU-X-Alliance CPA", "campaign", ["abm-cloud-gpu"], ["cloud-gpu-x-alliance"], "any"),
    ("campaign phải chứa cả cloud và gpu exclude draft", "campaign", ["cloud", "gpu"], ["draft"], "all"),
    ("campaign CloudGPUX trừ <script>", "campaign", ["gg-search-cloud-gpu-x"], [], "any"),
]

RAW_LLM_HOSTILE_MESSAGES = [
    ("GPUaaS là gì và có nên thuê không", "url"),
    ("GPUaaS nên định giá ra sao cho enterprise", "url"),
    ("MLOps khác K8s như thế nào", "url"),
    ("K8s H200 là tài liệu kỹ thuật hay landing page", "url"),
    ("BrandLiftQ3 có nghĩa là gì trong marketing", "campaign"),
    ("ABMCloudGPU là loại campaign gì", "campaign"),
    ("đừng lọc CloudGPUX dù có nói CTR", "url"),
    ("không cần filter campaign H100Readers, hỏi audience thôi", "campaign"),
]
RAW_LLM_HOSTILE_SPECS = [
    {"scope": "campaign", "include": ["GPUaaS", "drop table", "../etc/passwd"], "exclude": ["CloudGPUX"], "match_mode": "all"},
    {"scope": "url", "include": ["MLOps", "metric", "capacity"], "exclude": ["../secret"], "match_mode": "all"},
    {"scope": "campaign", "include": ["BrandLiftQ3", "<script>"], "exclude": ["unmentioned"], "match_mode": "all"},
    {"scope": "url", "include": ["H100Readers", "leads", "roas"], "exclude": ["../../tmp"], "match_mode": "all"},
]
RAW_LLM_HOSTILE_NO_FILTER_CASES = [
    (message, scope, raw)
    for message, scope in RAW_LLM_HOSTILE_MESSAGES
    for raw in RAW_LLM_HOSTILE_SPECS
]

RAW_LLM_CANONICAL_BASES = [
    (
        "url CloudGPUX traffic",
        "url",
        ["product/cloud-gpu-x"],
        [],
        [
            {"scope": "campaign", "include": ["CloudGPUX", "gg-search-cloud-gpu-x", "../etc"], "exclude": ["unmentioned"], "match_mode": "all"},
            {"scope": "url", "include": ["CloudGPUX", "traffic", "drop table"], "exclude": ["private"], "match_mode": "all"},
            {"scope": "campaign", "terms": ["CloudGPUX", "spend"], "exclude": ["GPUaaS"], "match_mode": "all"},
            {"scope": "url", "url_terms": ["CloudGPUX", "catalog"], "exclude_url_terms": ["../x"], "match_mode": "all"},
        ],
    ),
    (
        "path /product/gpuaas conversion",
        "url",
        ["product/gpuaas"],
        [],
        [
            {"scope": "url", "include": ["product/gpuaas", "conversion"], "exclude": ["drop table"], "match_mode": "all"},
            {"scope": "campaign", "include": ["GPUaaS", "pmax-gpuaas-q2"], "exclude": ["../secret"], "match_mode": "all"},
            {"scope": "url", "terms": ["product/gpuaas", "metric"], "exclude": ["CloudGPUX"], "match_mode": "all"},
            {"scope": "url", "url_terms": ["/product/gpuaas", "capacity"], "exclude_url_terms": ["<script>"], "match_mode": "all"},
        ],
    ),
    (
        "url CloudGPUX trừ GPUaaS traffic",
        "url",
        ["product/cloud-gpu-x"],
        ["product/gpuaas"],
        [
            {"scope": "url", "include": ["CloudGPUX", "traffic", "H200"], "exclude": ["GPUaaS", "../x"], "match_mode": "all"},
            {"scope": "campaign", "include": ["gg-search-cloud-gpu-x", "CloudGPUX"], "exclude": ["pmax-gpuaas-q2", "GPUaaS"], "match_mode": "all"},
            {"scope": "url", "terms": ["CloudGPUX", "drop-table"], "exclude_url_terms": ["GPUaaS", "private"], "match_mode": "all"},
            {"scope": "url", "url_terms": ["product/cloud-gpu-x"], "exclude": ["GPUaaS", "conversion"], "match_mode": "all"},
        ],
    ),
    (
        "campaign CloudGPUX spend",
        "campaign",
        ["gg-search-cloud-gpu-x"],
        [],
        [
            {"scope": "url", "include": ["product/cloud-gpu-x", "CloudGPUX", "spend"], "exclude": ["../etc"], "match_mode": "all"},
            {"scope": "campaign", "include": ["CloudGPUX", "spend", "drop table"], "exclude": ["GPUaaS"], "match_mode": "all"},
            {"scope": "url", "terms": ["CloudGPUX", "traffic"], "exclude": ["private"], "match_mode": "all"},
            {"scope": "campaign", "campaign_terms": ["CloudGPUX"], "exclude": ["<script>"], "match_mode": "all"},
        ],
    ),
    (
        "campaign ABMCloudGPU trừ BrandLiftQ3 CPA",
        "campaign",
        ["abm-cloud-gpu"],
        ["brandlift-q3"],
        [
            {"scope": "campaign", "include": ["ABMCloudGPU", "CPA"], "exclude": ["BrandLiftQ3", "<script>"], "match_mode": "all"},
            {"scope": "url", "include": ["solutions/abm-cloud-gpu", "ABMCloudGPU"], "exclude": ["case-study/brandlift-q3"], "match_mode": "all"},
            {"scope": "campaign", "terms": ["ABMCloudGPU", "leads"], "exclude": ["BrandLiftQ3", "drop"], "match_mode": "all"},
            {"scope": "campaign", "campaign_terms": ["ABMCloudGPU"], "exclude_url_terms": ["BrandLiftQ3"], "match_mode": "all"},
        ],
    ),
    (
        "campaign phải chứa cả cloud và gpu",
        "campaign",
        ["cloud", "gpu"],
        [],
        [
            {"scope": "campaign", "include": ["cloud", "gpu", "drop-table"], "exclude": ["../x"], "match_mode": "any"},
            {"scope": "url", "include": ["cloud", "gpu", "product/cloud-gpu-x"], "exclude": ["private"], "match_mode": "any"},
            {"scope": "campaign", "terms": ["cloud", "gpu", "spend"], "exclude": ["BrandLiftQ3"], "match_mode": "any"},
            {"scope": "campaign", "campaign_terms": ["cloud", "gpu"], "exclude": ["<script>"], "match_mode": "any"},
        ],
    ),
]
RAW_LLM_CANONICAL_CASES = [
    (message, scope, raw, include, exclude, "all" if "phải chứa cả" in message else "any")
    for message, scope, include, exclude, raws in RAW_LLM_CANONICAL_BASES
    for raw in raws
]

MATCH_MODE_AND_SECURITY_CASES = [
    ("url phải chứa cả CloudGPUX và GPUaaS", "url", ["product/cloud-gpu-x", "product/gpuaas"], [], "all"),
    ("url match all CloudGPUX và H100Readers", "url", ["product/cloud-gpu-x", "blog/h100-readers"], [], "all"),
    ("url tất cả keyword cloud và gpu", "url", ["cloud", "gpu"], [], "all"),
    ("campaign phải chứa cả CloudGPUX và H100Readers", "campaign", ["gg-search-cloud-gpu-x", "remarketing-h100-readers"], [], "all"),
    ("campaign match all brand và lift", "campaign", ["brand", "lift"], [], "all"),
    ("campaign tất cả keyword cloud và gpu", "campaign", ["cloud", "gpu"], [], "all"),
    ("url catalog CloudGPUX và GPUaaS", "url", ["catalog", "product/cloud-gpu-x", "product/gpuaas"], [], "any"),
    ("url capacity CloudGPUX và MLOps", "url", ["capacity", "product/cloud-gpu-x", "solutions/mlops-control-plane"], [], "any"),
    ("url case CloudGPUX và H100Readers", "url", ["case", "product/cloud-gpu-x", "blog/h100-readers"], [], "any"),
    ("url alliance CloudGPUX và ABMCloudGPU", "url", ["alliance", "product/cloud-gpu-x", "solutions/abm-cloud-gpu"], [], "any"),
    ("campaign catalog CloudGPUX và GPUaaS", "campaign", ["catalog", "gg-search-cloud-gpu-x", "pmax-gpuaas-q2"], [], "any"),
    ("campaign capacity MLOps và H100Readers", "campaign", ["capacity", "mlops-k8s-h200", "remarketing-h100-readers"], [], "any"),
    ("campaign case BrandLiftQ3 và ABMCloudGPU", "campaign", ["case", "brandlift-q3", "abm-cloud-gpu"], [], "any"),
    ("campaign alliance CloudGPUX", "campaign", ["alliance", "gg-search-cloud-gpu-x"], [], "any"),
    ("url CloudGPUX drop table traffic", "url", ["product/cloud-gpu-x"], [], "any"),
    ("url CloudGPUX ../private traffic", "url", ["product/cloud-gpu-x"], [], "any"),
    ("url CloudGPUX <script> traffic", "url", ["product/cloud-gpu-x"], [], "any"),
    ('url "drop-table" traffic', "url", ["drop-table"], [], "any"),
    ('url "../private" traffic', "url", [], [], "any"),
    ('campaign "drop-table" spend', "campaign", ["drop-table"], [], "any"),
    ("campaign CloudGPUX drop table spend", "campaign", ["gg-search-cloud-gpu-x"], [], "any"),
    ("campaign CloudGPUX ../private spend", "campaign", ["gg-search-cloud-gpu-x"], [], "any"),
    ("campaign CloudGPUX <script> spend", "campaign", ["gg-search-cloud-gpu-x"], [], "any"),
    ("url phải chứa cả MLOps và K8s H200", "url", ["solutions/mlops-control-plane", "docs/k8s-h200-gpu"], [], "all"),
    ("url match all BrandLiftQ3 và ABMCloudGPU", "url", ["case-study/brandlift-q3", "solutions/abm-cloud-gpu"], [], "all"),
    ("url tất cả keyword h200 và h100", "url", ["h200", "h100"], [], "all"),
    ("campaign phải chứa cả GPUaaS và BrandLiftQ3", "campaign", ["pmax-gpuaas-q2", "brandlift-q3"], [], "all"),
    ("campaign match all h100 và readers", "campaign", ["h100", "readers"], [], "all"),
    ("campaign tất cả keyword abm và cloud", "campaign", ["abm", "cloud"], [], "all"),
    ("url catalog MLOps và K8s H200", "url", ["catalog", "solutions/mlops-control-plane", "docs/k8s-h200-gpu"], [], "any"),
    ("url capacity GPUaaS và H100Readers", "url", ["capacity", "product/gpuaas", "blog/h100-readers"], [], "any"),
    ("url case BrandLiftQ3 và CloudGPUX", "url", ["case", "case-study/brandlift-q3", "product/cloud-gpu-x"], [], "any"),
    ("url alliance ABMCloudGPU và MLOps", "url", ["alliance", "solutions/abm-cloud-gpu", "solutions/mlops-control-plane"], [], "any"),
    ("campaign catalog GPUaaS và BrandLiftQ3", "campaign", ["catalog", "pmax-gpuaas-q2", "brandlift-q3"], [], "any"),
    ("campaign capacity CloudGPUX và ABMCloudGPU", "campaign", ["capacity", "gg-search-cloud-gpu-x", "abm-cloud-gpu"], [], "any"),
    ("campaign case H100Readers và MLOps", "campaign", ["case", "remarketing-h100-readers", "mlops-k8s-h200"], [], "any"),
    ("campaign alliance Cloud-GPU-X-Alliance", "campaign", ["cloud-gpu-x-alliance"], [], "any"),
    ("url GPUaaS drop table conversion", "url", ["product/gpuaas"], [], "any"),
    ("url MLOps ../shadow ranking", "url", ["solutions/mlops-control-plane"], [], "any"),
    ("url H100Readers <script> CTR", "url", ["blog/h100-readers"], [], "any"),
    ('url "drop-table" và CloudGPUX traffic', "url", ["drop-table", "product/cloud-gpu-x"], [], "any"),
    ('url "../../shadow" và GPUaaS traffic', "url", ["product/gpuaas"], [], "any"),
    ('campaign "drop-table" và CloudGPUX spend', "campaign", ["drop-table", "gg-search-cloud-gpu-x"], [], "any"),
    ("campaign GPUaaS drop table spend", "campaign", ["pmax-gpuaas-q2"], [], "any"),
    ("campaign MLOps ../shadow leads", "campaign", ["mlops-k8s-h200"], [], "any"),
    ("campaign H100Readers <script> CTR", "campaign", ["remarketing-h100-readers"], [], "any"),
]

ACRONYM_GLUED_CASES = [
    ("CloudGPUX traffic", "url", ["product/cloud-gpu-x"], [], "any"),
    ("Cloud-GPU-X traffic", "url", ["product/cloud-gpu-x"], [], "any"),
    ("GPUaaS conversion", "url", ["product/gpuaas"], [], "any"),
    ("MLOps ranking", "url", ["solutions/mlops-control-plane"], [], "any"),
    ("K8s H200 score", "url", ["docs/k8s-h200-gpu"], [], "any"),
    ("H200 LCP", "url", ["docs/k8s-h200-gpu"], [], "any"),
    ("H100Readers CTR organic", "url", ["blog/h100-readers"], [], "any"),
    ("BrandLiftQ3 users", "url", ["case-study/brandlift-q3"], [], "any"),
    ("ABMCloudGPU conversion", "url", ["solutions/abm-cloud-gpu"], [], "any"),
    ("H200 vs H100 ranking", "url", ["blog/h200-vs-h100"], [], "any"),
    ("url chứa CloudGPUXPlus", "url", ["cloud-gpuxplus"], [], "any"),
    ("url chứa GPUaaSStarter", "url", ["gpuaasstarter"], [], "any"),
    ("url chứa MLOpsKit", "url", ["mlopskit"], [], "any"),
    ("url chứa K8sH200Quickstart", "url", ["k8s-h200quickstart"], [], "any"),
    ("url chứa H100ReadersPlus", "url", ["h100-readers-plus"], [], "any"),
    ("url phải chứa cả CloudGPUXPlus và signup", "url", ["cloud-gpuxplus", "signup"], [], "all"),
    ("campaign CloudGPUX spend", "campaign", ["gg-search-cloud-gpu-x"], [], "any"),
    ("campaign Cloud-GPU-X CPA", "campaign", ["gg-search-cloud-gpu-x"], [], "any"),
    ("campaign GPUaaS ROAS", "campaign", ["pmax-gpuaas-q2"], [], "any"),
    ("campaign MLOps leads", "campaign", ["mlops-k8s-h200"], [], "any"),
    ("campaign K8s H200 cost", "campaign", ["mlops-k8s-h200"], [], "any"),
    ("campaign H200 spend", "campaign", ["mlops-k8s-h200"], [], "any"),
    ("campaign H100Readers CTR", "campaign", ["remarketing-h100-readers"], [], "any"),
    ("campaign BrandLiftQ3 reach", "campaign", ["brandlift-q3"], [], "any"),
    ("campaign ABMCloudGPU leads", "campaign", ["abm-cloud-gpu"], [], "any"),
    ("campaign Cloud-GPU-X-Alliance CPA", "campaign", ["cloud-gpu-x-alliance"], [], "any"),
    ("campaign chứa CloudGPUXPlus", "campaign", ["cloud-gpuxplus"], [], "any"),
    ("campaign chứa GPUaaSStarter", "campaign", ["gpuaasstarter"], [], "any"),
    ("campaign chứa MLOpsKit", "campaign", ["mlopskit"], [], "any"),
    ("campaign chứa K8sH200Quickstart", "campaign", ["k8s-h200quickstart"], [], "any"),
    ("campaign chứa H100ReadersPlus", "campaign", ["h100-readers-plus"], [], "any"),
    ("campaign phải chứa cả CloudGPUXPlus và trial", "campaign", ["cloud-gpuxplus", "trial"], [], "all"),
]

CASE_GROUPS = [
    LONG_MIXED_CLAUSE_CASES,
    PUNCTUATION_AND_SPACING_CASES,
    SCOPE_AMBIGUITY_CASES,
    NEGATION_SUPPRESSION_CASES,
    TRUE_EXCLUSION_CASES,
    RAW_LLM_HOSTILE_NO_FILTER_CASES,
    RAW_LLM_CANONICAL_CASES,
    MATCH_MODE_AND_SECURITY_CASES,
    ACRONYM_GLUED_CASES,
]
ROUND5_SUBCASE_COUNT = sum(len(group) for group in CASE_GROUPS)


class IntentFilterHardeningRound5Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_catalog = entity_resolver.catalog
        entity_resolver.catalog = fake_catalog

    @classmethod
    def tearDownClass(cls):
        entity_resolver.catalog = cls._orig_catalog

    def spec(self, message, action="seo_query", scope="url", extra=None):
        data = {"action": action}
        if extra:
            data.update(extra)
        return server._filter_spec_from_action(data, message, scope)

    def assertNoFilter(self, message, *, action="seo_query", scope="url", extra=None):
        spec = self.spec(message, action=action, scope=scope, extra=extra)
        self.assertEqual(scope, spec.get("scope"), msg=f"{message!r} => {spec}")
        self.assertEqual([], spec.get("include") or [], msg=f"{message!r} => {spec}")
        self.assertEqual([], spec.get("exclude") or [], msg=f"{message!r} => {spec}")
        self.assertFalse(server._filter_active(spec), msg=f"{message!r} => {spec}")
        return spec

    def assertFilter(
        self,
        message,
        include,
        *,
        exclude=None,
        action="seo_query",
        scope="url",
        match_mode="any",
        extra=None,
    ):
        spec = self.spec(message, action=action, scope=scope, extra=extra)
        self.assertEqual(scope, spec.get("scope"), msg=f"{message!r} => {spec}")
        self.assertEqual(list(include), spec.get("include") or [], msg=f"{message!r} => {spec}")
        self.assertEqual(list(exclude or []), spec.get("exclude") or [], msg=f"{message!r} => {spec}")
        self.assertEqual(match_mode, spec.get("match_mode"), msg=f"{message!r} => {spec}")
        self.assertTrue(server._filter_active(spec), msg=f"{message!r} => {spec}")
        return spec

    def test_round5_subcase_budget(self):
        self.assertGreaterEqual(ROUND5_SUBCASE_COUNT, 280)
        self.assertLessEqual(ROUND5_SUBCASE_COUNT, 360)

    def test_long_mixed_clauses_keep_metric_entity_filters(self):
        for message, scope, include, exclude, match_mode in LONG_MIXED_CLAUSE_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_spacing_quotes_slashes_colons_parentheses_and_semicolons(self):
        for message, scope, include, exclude, match_mode in PUNCTUATION_AND_SPACING_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_scope_and_alias_collisions_are_decided_by_action_scope(self):
        for message, scope, include, exclude, match_mode in SCOPE_AMBIGUITY_CASES:
            with self.subTest(message=message):
                if include or exclude:
                    self.assertFilter(
                        message,
                        include,
                        exclude=exclude,
                        action="ads_perf" if scope == "campaign" else "seo_query",
                        scope=scope,
                        match_mode=match_mode,
                    )
                else:
                    self.assertNoFilter(
                        message,
                        action="ads_perf" if scope == "campaign" else "seo_query",
                        scope=scope,
                    )

    def test_negated_mentions_are_suppressed_but_true_excludes_survive(self):
        for message, action, scope in NEGATION_SUPPRESSION_CASES:
            with self.subTest(message=message, kind="suppressed"):
                self.assertNoFilter(message, action=action, scope=scope)

        for message, scope, include, exclude, match_mode in TRUE_EXCLUSION_CASES:
            with self.subTest(message=message, kind="exclude"):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_raw_llm_filter_spec_drops_hostile_or_ungrounded_terms(self):
        for message, scope, raw in RAW_LLM_HOSTILE_NO_FILTER_CASES:
            with self.subTest(message=message):
                self.assertNoFilter(
                    message,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    extra={"filter_spec": raw},
                )

    def test_raw_llm_filter_spec_keeps_only_user_grounded_canonical_terms(self):
        for message, scope, raw, include, exclude, match_mode in RAW_LLM_CANONICAL_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                    extra={"filter_spec": raw},
                )

    def test_match_mode_requires_explicit_all_and_security_terms_are_not_accidental_filters(self):
        for message, scope, include, exclude, match_mode in MATCH_MODE_AND_SECURITY_CASES:
            with self.subTest(message=message):
                if include or exclude:
                    self.assertFilter(
                        message,
                        include,
                        exclude=exclude,
                        action="ads_perf" if scope == "campaign" else "seo_query",
                        scope=scope,
                        match_mode=match_mode,
                    )
                else:
                    self.assertNoFilter(
                        message,
                        action="ads_perf" if scope == "campaign" else "seo_query",
                        scope=scope,
                    )

    def test_acronym_camel_glued_and_free_terms(self):
        for message, scope, include, exclude, match_mode in ACRONYM_GLUED_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )


if __name__ == "__main__":
    unittest.main()
