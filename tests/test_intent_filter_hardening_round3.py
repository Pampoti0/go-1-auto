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
        "aliases": ["product", "san pham", "sản phẩm", "trang san pham", "trang sản phẩm"],
        "patterns": ["product"],
    },
    {
        "id": "url:blog",
        "label": "blog",
        "scope": "url",
        "type": "url_group",
        "aliases": ["blog", "bai blog", "bài blog", "content blog"],
        "patterns": ["blog"],
    },
    {
        "id": "url:gpu-server",
        "label": "gpu server",
        "scope": "url",
        "type": "url",
        "aliases": [
            "gpu server",
            "gpu-server",
            "product gpu server",
            "product/gpu-server",
            "gpu server page",
        ],
        "patterns": ["product/gpu-server"],
    },
    {
        "id": "url:gpu-server-pro",
        "label": "gpu server pro",
        "scope": "url",
        "type": "url",
        "aliases": [
            "gpu server pro",
            "gpu-server-pro",
            "product gpu server pro",
            "product/gpu-server-pro",
            "gpu server pro page",
        ],
        "patterns": ["product/gpu-server-pro"],
    },
    {
        "id": "url:ai",
        "label": "ai",
        "scope": "url",
        "type": "url_group",
        "aliases": ["ai"],
        "patterns": ["ai"],
    },
    {
        "id": "url:ai-inference",
        "label": "ai inference",
        "scope": "url",
        "type": "url",
        "aliases": [
            "ai inference",
            "ai-inference",
            "solutions ai inference",
            "solutions/ai-inference",
            "ai inference solution",
        ],
        "patterns": ["solutions/ai-inference"],
    },
    {
        "id": "url:gpu-benchmark",
        "label": "gpu benchmark",
        "scope": "url",
        "type": "url",
        "aliases": [
            "gpu benchmark",
            "gpu-benchmark",
            "resources gpu benchmark",
            "resources/gpu-benchmark",
            "benchmark gpu",
        ],
        "patterns": ["resources/gpu-benchmark"],
    },
    {
        "id": "url:h100-price",
        "label": "h100 price",
        "scope": "url",
        "type": "url",
        "aliases": ["h100 price", "h100-price", "blog h100 price", "blog/h100-price"],
        "patterns": ["blog/h100-price"],
    },
    {
        "id": "url:h200-vs-h100",
        "label": "h200 vs h100",
        "scope": "url",
        "type": "url",
        "aliases": [
            "h200 vs h100",
            "h200-vs-h100",
            "h200 versus h100",
            "blog h200 vs h100",
            "blog/h200-vs-h100",
        ],
        "patterns": ["blog/h200-vs-h100"],
    },
    {
        "id": "url:docs-api-gpu",
        "label": "docs api gpu",
        "scope": "url",
        "type": "url",
        "aliases": [
            "api gpu",
            "api-gpu",
            "docs api gpu",
            "docs/api-gpu",
            "gpu api docs",
        ],
        "patterns": ["docs/api-gpu"],
    },
    {
        "id": "url:case-study-viettel-ai",
        "label": "case study viettel ai",
        "scope": "url",
        "type": "url",
        "aliases": [
            "case study viettel ai",
            "case-study viettel ai",
            "case-study/viettel-ai",
            "viettel ai case study",
        ],
        "patterns": ["case-study/viettel-ai"],
    },
    {
        "id": "campaign:brand",
        "label": "brand campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["brand", "brand campaign", "decho", "decho brand"],
        "patterns": ["decho"],
    },
    {
        "id": "campaign:competitor",
        "label": "competitor campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["competitor", "doi thu", "đối thủ", "fpt", "viettel"],
        "patterns": ["fpt", "viettel"],
    },
    {
        "id": "campaign:gg-search-h100-price",
        "label": "GG_Search_H100_Price",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "gg search h100 price",
            "gg_search_h100_price",
            "search h100 price",
            "h100 price",
            "h100 price campaign",
        ],
        "patterns": ["GG_Search_H100_Price"],
    },
    {
        "id": "campaign:gg-search-ai-inference",
        "label": "GG_Search_AI_Inference",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "gg search ai inference",
            "gg_search_ai_inference",
            "search ai inference",
            "ai inference",
            "ai inference campaign",
        ],
        "patterns": ["GG_Search_AI_Inference"],
    },
    {
        "id": "campaign:competitor-viettel-ai",
        "label": "Competitor_Viettel_AI",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "competitor viettel ai",
            "competitor_viettel_ai",
            "viettel ai",
            "viettel ai campaign",
        ],
        "patterns": ["Competitor_Viettel_AI"],
    },
    {
        "id": "campaign:brand-decho-awareness",
        "label": "Brand_DeCho_Awareness",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "brand decho awareness",
            "brand_decho_awareness",
            "decho awareness",
            "brand awareness",
        ],
        "patterns": ["Brand_DeCho_Awareness"],
    },
    {
        "id": "campaign:pmax-gpu-server-trial",
        "label": "PMAX_GPU_Server_Trial",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "pmax gpu server trial",
            "pmax_gpu_server_trial",
            "gpu server trial",
            "server trial campaign",
        ],
        "patterns": ["PMAX_GPU_Server_Trial"],
    },
    {
        "id": "campaign:remarketing-h100-readers",
        "label": "Remarketing_H100_Readers",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "remarketing h100 readers",
            "remarketing_h100_readers",
            "h100 readers",
            "h100 remarketing",
        ],
        "patterns": ["Remarketing_H100_Readers"],
    },
]


def fake_catalog(urls=None):
    return deepcopy(FAKE_ENTITIES)


OVERLAP_SPECIFICITY_CASES = [
    ("gpu server pro performance", ["product/gpu-server-pro"]),
    ("GPU Server Pro traffic thế nào", ["product/gpu-server-pro"]),
    ("product gpu server pro có conversion không", ["product/gpu-server-pro"]),
    ("báo cáo GPU-server-pro", ["product/gpu-server-pro"]),
    ("trang gpu server pro có chậm không", ["product/gpu-server-pro"]),
    ("url /product/gpu-server-pro", ["product/gpu-server-pro"]),
    ("path product/gpu-server-pro", ["product/gpu-server-pro"]),
    ("gpu server pro và ai inference", ["product/gpu-server-pro", "solutions/ai-inference"]),
    ("gpu server pro trừ gpu server", ["product/gpu-server-pro"], ["product/gpu-server"]),
    ("gpu server pro không gồm gpu server", ["product/gpu-server-pro"], ["product/gpu-server"]),
    ("gpu server traffic", ["product/gpu-server"]),
    ("product/gpu-server pagespeed", ["product/gpu-server"]),
    ("ai inference convert ra sao", ["solutions/ai-inference"]),
    ("AI inference solution organic thế nào", ["solutions/ai-inference"]),
    ("solutions/ai-inference có LCP cao không", ["solutions/ai-inference"]),
    ("ai inference trừ generic ai", ["solutions/ai-inference"], ["ai"]),
    ("gpu benchmark traffic", ["resources/gpu-benchmark"]),
    ("resources/gpu-benchmark score", ["resources/gpu-benchmark"]),
    ("docs api gpu lỗi tracking không", ["docs/api-gpu"]),
    ("api-gpu docs có users không", ["docs/api-gpu"]),
    ("case study viettel ai organic", ["case-study/viettel-ai"]),
    ("viettel ai case study conversion", ["case-study/viettel-ai"]),
    ("h200 vs h100 traffic", ["blog/h200-vs-h100"]),
    ("blog h200 vs h100 CTR thấp không", ["blog/h200-vs-h100"]),
]

NO_FILTER_NATURAL_CASES = [
    ("giá H100 thế nào", "seo_query", "url"),
    ("H100 bao nhiêu tiền", "seo_query", "url"),
    ("H100 price market hiện nay", "seo_query", "url"),
    ("h100 price thế nào nếu mua cloud", "seo_query", "url"),
    ("so sánh giá H100 ngoài thị trường", "seo_query", "url"),
    ("nên mua H100 hay thuê H200", "seo_query", "url"),
    ("H200 vs H100 nên chọn con nào", "seo_query", "url"),
    ("AI inference là gì", "seo_query", "url"),
    ("gpu benchmark nghĩa là gì", "seo_query", "url"),
    ("API GPU cần tài liệu nào", "seo_query", "url"),
    ("Viettel AI là đối tác hay đối thủ", "seo_query", "url"),
    ("case study Viettel AI bên ngoài có gì hay", "seo_query", "url"),
    ("GPU server pro là cấu hình gì", "seo_query", "url"),
    ("đừng lọc product", "seo_query", "url"),
    ("đừng filter gpu server pro", "seo_query", "url"),
    ("không cần lọc blog", "seo_query", "url"),
    ("không phải h100 price", "seo_query", "url"),
    ("not h100 price, hỏi tổng quan thôi", "seo_query", "url"),
    ("không phải campaign H100 price", "ads_perf", "campaign"),
    ("đừng lọc campaign brand", "ads_perf", "campaign"),
    ("ads gpu server pro performance", "ads_perf", "campaign"),
    ("ads ai inference tổng quan", "ads_perf", "campaign"),
    ("campaigner nói về viettel ai", "ads_perf", "campaign"),
    ("landingness của gpu server", "seo_query", "url"),
    ("urlencoded h100 price test", "seo_query", "url"),
    ("pathological gpu server pro copy", "seo_query", "url"),
    ("filtering h100 price trong prompt", "seo_query", "url"),
    ("ViettelAI performance", "ads_perf", "campaign"),
]

EXPLICIT_CUE_NO_FILTER_CASES = [
    ("urlencoded custom-slug report", "seo_query", "url"),
    ("preurl custom-slug status", "seo_query", "url"),
    ("urlify custom slug report", "seo_query", "url"),
    ("pathological custom-slug traffic", "seo_query", "url"),
    ("pathfinder custom benchmark idea", "seo_query", "url"),
    ("campaigner viettel-ai workload", "ads_perf", "campaign"),
    ("campaigning custom readers idea", "ads_perf", "campaign"),
    ("landingness custom metric", "seo_query", "url"),
    ("filtering custom-slug copy", "seo_query", "url"),
    ("filtered custom price example", "seo_query", "url"),
    ("urlencoded not-a-real-pathless word", "seo_query", "url"),
    ("pathological exact-name note", "ads_perf", "campaign"),
    ("campaignish custom readers", "ads_perf", "campaign"),
    ("landingish custom idea", "seo_query", "url"),
    ("ldper custom slug", "seo_query", "url"),
    ("urlness custom metric", "seo_query", "url"),
    ("pathway custom inference overview", "seo_query", "url"),
    ("campaignless custom report", "ads_perf", "campaign"),
    ("filterable h100 price narrative", "seo_query", "url"),
    ("nonurl custom benchmark mention", "seo_query", "url"),
    ("apath custom docs note", "seo_query", "url"),
    ("campaigner Custom_Brand_Awareness", "ads_perf", "campaign"),
    ("landingness PMAX_Custom_Trial", "seo_query", "url"),
    ("urlencoded GG_Search_Custom_Name", "seo_query", "url"),
]

EXPLICIT_CUE_FILTER_CASES = [
    ("url chứa custom-slug", "url", ["custom-slug"], [], "any"),
    ("lọc URL custom_slug", "url", ["custom_slug"], [], "any"),
    ("URL custom/path", "url", ["custom/path"], [], "any"),
    ("path /custom/path", "url", ["custom/path"], [], "any"),
    ("url chứa /product/gpu-server-pro", "url", ["product/gpu-server-pro"], [], "any"),
    ("path product/gpu-server-pro", "url", ["product/gpu-server-pro"], [], "any"),
    ("url chứa /solutions/ai-inference", "url", ["solutions/ai-inference"], [], "any"),
    ("trang /resources/gpu-benchmark", "url", ["resources/gpu-benchmark"], [], "any"),
    ("url /blog/h200-vs-h100", "url", ["blog/h200-vs-h100"], [], "any"),
    ("url chứa docs/api-gpu", "url", ["docs/api-gpu"], [], "any"),
    ("url chứa case-study/viettel-ai", "url", ["case-study/viettel-ai"], [], "any"),
    ("url chứa custom-slug ngoại trừ staging", "url", ["custom-slug"], ["staging"], "any"),
    ("trừ URL staging", "url", [], ["staging"], "any"),
    ("campaign chứa exact-name", "campaign", ["exact-name"], [], "any"),
    ("lọc campaign summer-sale", "campaign", ["summer-sale"], [], "any"),
    ("campaign custom_name", "campaign", ["custom_name"], [], "any"),
    ("campaign ViettelAI", "campaign", ["viettelai"], [], "any"),
    ("campaign chứa viettel-ai", "campaign", ["competitor-viettel-ai"], [], "any"),
    ("campaign GG_Search_H100_Price", "campaign", ["gg-search-h100-price"], [], "any"),
    ("campaign chứa GG_Search_AI_Inference", "campaign", ["gg-search-ai-inference"], [], "any"),
    ("campaign Brand_DeCho_Awareness", "campaign", ["brand-de-cho-awareness"], [], "any"),
    ("campaign PMAX_GPU_Server_Trial", "campaign", ["pmax-gpu-server-trial"], [], "any"),
    ("campaign Remarketing_H100_Readers", "campaign", ["remarketing-h100-readers"], [], "any"),
    ("campaign chứa exact-name ngoại trừ draft", "campaign", ["exact-name"], ["draft"], "any"),
    ("campaign phải chứa cả brand-x và prospecting", "campaign", ["brand-x", "prospecting"], [], "all"),
    ("url phải chứa cả feature-x và signup", "url", ["feature-x", "signup"], [], "all"),
    ("path /docs/api-gpu except preview", "url", ["docs/api-gpu"], ["preview"], "any"),
    ("campaign chứa ViettelAI exclude draft", "campaign", ["viettelai"], ["draft"], "any"),
    ("url chứa 'enterprise-landing'", "url", ["enterprise-landing"], [], "any"),
    ('campaign chứa "brand-x"', "campaign", ["brand-x"], [], "any"),
]

NEGATION_EXCLUSION_CASES = [
    ("gpu server pro trừ gpu server", "url", ["product/gpu-server-pro"], ["product/gpu-server"], "any"),
    ("gpu server pro ngoại trừ gpu server", "url", ["product/gpu-server-pro"], ["product/gpu-server"], "any"),
    ("gpu server pro không gồm gpu server", "url", ["product/gpu-server-pro"], ["product/gpu-server"], "any"),
    ("gpu server pro except gpu server", "url", ["product/gpu-server-pro"], ["product/gpu-server"], "any"),
    ("gpu server pro exclude gpu server", "url", ["product/gpu-server-pro"], ["product/gpu-server"], "any"),
    ('"gpu server pro" trừ "gpu server"', "url", ["product/gpu-server-pro"], ["product/gpu-server"], "any"),
    ("ai inference trừ ai", "url", ["solutions/ai-inference"], ["ai"], "any"),
    ("ai inference không gồm gpu benchmark", "url", ["solutions/ai-inference"], ["resources/gpu-benchmark"], "any"),
    ("gpu benchmark ngoại trừ docs api gpu", "url", ["resources/gpu-benchmark"], ["docs/api-gpu"], "any"),
    ("docs api gpu except gpu benchmark", "url", ["docs/api-gpu"], ["resources/gpu-benchmark"], "any"),
    ("case study viettel ai trừ ai inference", "url", ["case-study/viettel-ai"], ["solutions/ai-inference"], "any"),
    ("blog h200 vs h100 ngoại trừ h100 price", "url", ["blog/h200-vs-h100"], ["blog/h100-price"], "any"),
    ("blog h100 price trừ h200 vs h100", "url", ["blog/h100-price"], ["blog/h200-vs-h100"], "any"),
    ("url /blog/h200-vs-h100 exclude /blog/h100-price", "url", ["blog/h200-vs-h100"], ["blog/h100-price"], "any"),
    ("url /product/gpu-server-pro không gồm /product/gpu-server", "url", ["product/gpu-server-pro"], ["product/gpu-server"], "any"),
    ("product trừ blog", "url", ["product"], ["blog"], "any"),
    ("blog h100 price không gồm blog h200 vs h100", "url", ["blog/h100-price"], ["blog/h200-vs-h100"], "any"),
    ("campaign Viettel AI trừ competitor", "campaign", ["competitor-viettel-ai"], ["fpt", "viettel"], "any"),
    ("campaign GG_Search_H100_Price trừ Remarketing_H100_Readers", "campaign", ["gg-search-h100-price"], ["remarketing-h100-readers"], "any"),
    ("campaign H100 price except h100 readers", "campaign", ["gg-search-h100-price"], ["remarketing-h100-readers"], "any"),
    ("campaign AI inference exclude h100 price", "campaign", ["gg-search-ai-inference"], ["gg-search-h100-price"], "any"),
    ("campaign Brand_DeCho_Awareness không gồm brand campaign", "campaign", ["brand-de-cho-awareness"], ["decho"], "any"),
    ("campaign PMAX_GPU_Server_Trial ngoại trừ Brand_DeCho_Awareness", "campaign", ["pmax-gpu-server-trial"], ["brand-de-cho-awareness"], "any"),
    ("campaign Remarketing_H100_Readers trừ GG_Search_H100_Price", "campaign", ["remarketing-h100-readers"], ["gg-search-h100-price"], "any"),
    ('campaign "viettel ai" except "fpt"', "campaign", ["competitor-viettel-ai"], ["fpt", "viettel"], "any"),
    ("không phải h100 price", "url", [], [], "any"),
    ("not h100 price", "url", [], [], "any"),
    ("không phải campaign H100 price", "campaign", [], [], "any"),
    ("not campaign Viettel AI", "campaign", [], [], "any"),
    ("đừng lọc product", "url", [], [], "any"),
    ("đừng lọc campaign brand", "campaign", [], [], "any"),
    ("không cần filter url gpu server pro", "url", [], [], "any"),
]

RAW_LLM_DROP_CASES = [
    ("giá H100 thế nào", "url", ["h100", "price", "latest", "analysis"]),
    ("H100 bao nhiêu tiền", "url", ["h100", "price", "campaign", "url"]),
    ("h100 price thế nào nếu mua cloud", "url", ["h100-price", "latest", "analysis"]),
    ("H200 vs H100 nên chọn con nào", "url", ["h100", "h200", "price", "latest"]),
    ("AI inference là gì", "url", ["ai", "inference", "analysis"]),
    ("gpu benchmark nghĩa là gì", "url", ["gpu", "benchmark", "url"]),
    ("API GPU cần tài liệu nào", "url", ["api", "gpu", "docs"]),
    ("urlencoded h100 price test", "url", ["h100-price", "url", "test"]),
    ("pathological gpu server pro copy", "url", ["gpu-server-pro", "path"]),
    ("filtering h100 price trong prompt", "url", ["filter", "h100-price"]),
    ("đừng lọc product", "url", ["product", "latest"]),
    ("không phải h100 price", "url", ["h100-price", "h100"]),
    ("not h100 price", "url", ["h100-price", "price"]),
    ("không cần lọc blog", "url", ["blog", "url"]),
    ("ads gpu server pro performance", "campaign", ["gpu-server-pro", "campaign"]),
    ("ads ai inference tổng quan", "campaign", ["ai-inference", "campaign"]),
    ("campaigner viettel-ai workload", "campaign", ["viettel", "viettel-ai"]),
    ("ViettelAI performance", "campaign", ["viettel", "viettelai"]),
    ("campaign nào đang chạy", "campaign", ["campaign", "running", "latest"]),
    ("Ads hiệu quả không", "campaign", ["ads", "campaign", "performance"]),
    ("SEO tổng quan toàn site", "url", ["seo", "site", "url"]),
    ("PageSpeed trang nào chậm", "url", ["pagespeed", "page", "slow"]),
    ("tracking conversion ổn không", "url", ["tracking", "conversion", "url"]),
    ("landing page nào convert tốt", "url", ["landing", "page", "convert"]),
    ("campaignless viettel ai report", "campaign", ["viettel-ai", "competitor"]),
    ("urlencoded GG_Search_H100_Price", "url", ["gg-search-h100-price", "h100-price"]),
    ("pathway ai inference overview", "url", ["ai-inference", "path"]),
    ("filterable h100 price narrative", "url", ["h100-price", "analysis"]),
    ("nonurl custom benchmark mention", "url", ["gpu-benchmark", "url"]),
    ("campaigning custom readers idea", "campaign", ["h100-readers", "remarketing"]),
    ("campaignish custom readers", "campaign", ["h100-readers", "campaign"]),
    ("landingness PMAX_Custom_Trial", "url", ["pmax-gpu-server-trial"]),
    ("urlify custom gpu serverish", "url", ["product/gpu-server", "url"]),
    ("campaigner Custom_Brand_Awareness", "campaign", ["brand-decho-awareness"]),
]

RAW_LLM_CANONICAL_CASES = [
    ("gpu server pro performance", "url", ["gpu server pro", "latest"], ["product/gpu-server-pro"], []),
    ("GPU Server Pro traffic thế nào", "url", ["gpu-server", "gpu-server-pro"], ["product/gpu-server-pro"], []),
    ("product gpu server pro có conversion không", "url", ["product/gpu-server-pro", "analysis"], ["product/gpu-server-pro"], []),
    ("ai inference convert ra sao", "url", ["ai", "ai inference", "latest"], ["solutions/ai-inference"], []),
    ("solutions/ai-inference có LCP cao không", "url", ["solutions/ai-inference", "url"], ["solutions/ai-inference"], []),
    ("gpu benchmark traffic", "url", ["gpu benchmark", "gpu"], ["resources/gpu-benchmark"], []),
    ("docs api gpu lỗi tracking không", "url", ["api gpu", "docs"], ["docs/api-gpu"], []),
    ("case study viettel ai organic", "url", ["case study viettel ai", "viettel"], ["case-study/viettel-ai"], []),
    ("blog h100 price traffic", "url", ["h100 price", "price", "latest"], ["blog/h100-price"], []),
    ("blog h200 vs h100 CTR thấp không", "url", ["h200 vs h100", "h100 price"], ["blog/h200-vs-h100"], []),
    ("url chứa /product/gpu-server-pro", "url", ["product/gpu-server-pro", "server"], ["product/gpu-server-pro"], []),
    ("url /blog/h200-vs-h100 exclude /blog/h100-price", "url", ["blog/h200-vs-h100", "h100"], ["blog/h200-vs-h100"], ["blog/h100-price"]),
    ("campaign GG_Search_H100_Price", "campaign", ["gg search h100 price", "price"], ["gg-search-h100-price"], []),
    ("campaign AI inference", "campaign", ["ai inference", "analysis"], ["gg-search-ai-inference"], []),
    ("campaign Viettel AI", "campaign", ["viettel", "viettel ai"], ["competitor-viettel-ai"], []),
    ("campaign Brand_DeCho_Awareness", "campaign", ["brand", "brand decho awareness"], ["brand-de-cho-awareness"], []),
    ("campaign PMAX_GPU_Server_Trial", "campaign", ["gpu server trial", "gpu"], ["pmax-gpu-server-trial"], []),
    ("campaign Remarketing_H100_Readers", "campaign", ["h100 readers", "h100"], ["remarketing-h100-readers"], []),
    ("campaign ViettelAI", "campaign", ["viettelai", "viettel"], ["viettelai"], []),
    ("campaign chứa exact-name", "campaign", ["exact-name", "latest"], ["exact-name"], []),
    ("campaign chứa exact-name ngoại trừ draft", "campaign", ["exact-name", "campaign"], ["exact-name"], ["draft"]),
    ("url chứa custom-slug", "url", ["custom-slug", "latest"], ["custom-slug"], []),
    ("path /custom/path", "url", ["custom/path", "path"], ["custom/path"], []),
    ("url chứa custom-slug ngoại trừ staging", "url", ["custom-slug", "url"], ["custom-slug"], ["staging"]),
]

CAMPAIGN_SPECIFICITY_CASES = [
    ("campaign GG_Search_H100_Price", ["gg-search-h100-price"]),
    ("GG_Search_H100_Price CPA cao không", ["gg-search-h100-price"]),
    ("campaign search h100 price", ["gg-search-h100-price"]),
    ("campaign h100 price", ["gg-search-h100-price"]),
    ("campaign GG_Search_AI_Inference", ["gg-search-ai-inference"]),
    ("GG_Search_AI_Inference conversions thế nào", ["gg-search-ai-inference"]),
    ("campaign AI inference", ["gg-search-ai-inference"]),
    ("search ai inference campaign", ["gg-search-ai-inference"]),
    ("campaign Viettel AI", ["competitor-viettel-ai"]),
    ("campaign Competitor_Viettel_AI", ["competitor-viettel-ai"]),
    ("Competitor_Viettel_AI CPA cao không", ["competitor-viettel-ai"]),
    ("campaign viettel-ai", ["competitor-viettel-ai"]),
    ("campaign chứa viettel-ai", ["competitor-viettel-ai"]),
    ("campaign ViettelAI", ["viettelai"]),
    ("campaign Brand_DeCho_Awareness", ["brand-de-cho-awareness"]),
    ("Brand_DeCho_Awareness reach ra sao", ["brand-de-cho-awareness"]),
    ("campaign decho awareness", ["brand-de-cho-awareness"]),
    ("campaign brand awareness", ["brand-de-cho-awareness"]),
    ("campaign PMAX_GPU_Server_Trial", ["pmax-gpu-server-trial"]),
    ("PMAX_GPU_Server_Trial cost thế nào", ["pmax-gpu-server-trial"]),
    ("campaign gpu server trial", ["pmax-gpu-server-trial"]),
    ("campaign Remarketing_H100_Readers", ["remarketing-h100-readers"]),
    ("Remarketing_H100_Readers CTR thế nào", ["remarketing-h100-readers"]),
    ("campaign h100 readers", ["remarketing-h100-readers"]),
    ("campaign h100 price và h100 readers", ["gg-search-h100-price", "remarketing-h100-readers"]),
    ("campaign ai inference hoặc viettel ai", ["gg-search-ai-inference", "competitor-viettel-ai"]),
    ("campaign brand", ["decho"]),
    ("campaign competitor", ["fpt", "viettel"]),
]

MULTI_KEYWORD_CASES = [
    ("gpu server pro, ai inference", "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "any"),
    ("gpu server pro và ai inference", "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "any"),
    ("gpu server pro hoặc ai inference", "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "any"),
    ("gpu server pro; ai inference", "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "any"),
    ('"gpu server pro" và "ai inference"', "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "any"),
    ("(gpu server pro) và (ai inference)", "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "any"),
    ("gpu benchmark, docs api gpu", "url", ["resources/gpu-benchmark", "docs/api-gpu"], [], "any"),
    ("blog h100 price hoặc blog h200 vs h100", "url", ["blog/h100-price", "blog/h200-vs-h100"], [], "any"),
    ("case study viettel ai và ai inference", "url", ["solutions/ai-inference", "case-study/viettel-ai"], [], "any"),
    ("url phải chứa cả /product/gpu-server-pro và /solutions/ai-inference", "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "all"),
    ("trang chứa cả gpu server pro và ai inference", "url", ["product/gpu-server-pro", "solutions/ai-inference"], [], "all"),
    ("path match all /resources/gpu-benchmark và /docs/api-gpu", "url", ["resources/gpu-benchmark", "docs/api-gpu"], [], "all"),
    ("url phải chứa cả custom-slug và signup", "url", ["custom-slug", "signup"], [], "all"),
    ("url custom-slug, signup", "url", ["custom-slug", "signup"], [], "any"),
    ("url custom-slug hoặc signup", "url", ["custom-slug", "signup"], [], "any"),
    ("campaign GG_Search_H100_Price, Remarketing_H100_Readers", "campaign", ["gg-search-h100-price", "remarketing-h100-readers"], [], "any"),
    ("campaign GG_Search_H100_Price và Remarketing_H100_Readers", "campaign", ["gg-search-h100-price", "remarketing-h100-readers"], [], "any"),
    ("campaign GG_Search_H100_Price hoặc Remarketing_H100_Readers", "campaign", ["gg-search-h100-price", "remarketing-h100-readers"], [], "any"),
    ("campaign GG_Search_AI_Inference; Competitor_Viettel_AI", "campaign", ["gg-search-ai-inference", "competitor-viettel-ai"], [], "any"),
    ('campaign "Viettel AI" và "AI inference"', "campaign", ["gg-search-ai-inference", "competitor-viettel-ai"], [], "any"),
    ("campaign phải chứa cả h100 và price", "campaign", ["h100", "price"], [], "all"),
    ("campaign match all h100 và readers", "campaign", ["h100", "readers"], [], "all"),
    ("campaign phải chứa cả brand-x và prospecting", "campaign", ["brand-x", "prospecting"], [], "all"),
    ("campaign brand-x, prospecting", "campaign", ["brand-x", "prospecting"], [], "any"),
    ("url /product/gpu-server-pro, /docs/api-gpu trừ /blog/h100-price", "url", ["product/gpu-server-pro", "docs/api-gpu"], ["blog/h100-price"], "any"),
    ("url phải chứa cả custom-slug và signup trừ archive", "url", ["custom-slug", "signup"], ["archive"], "all"),
    ("campaign GG_Search_H100_Price và Remarketing_H100_Readers trừ draft", "campaign", ["gg-search-h100-price", "remarketing-h100-readers"], ["draft"], "any"),
    ("campaign match all h100 và price exclude draft", "campaign", ["h100", "price"], ["draft"], "all"),
]

SCOPE_SAFETY_CASES = [
    ("ads gpu server pro performance", "campaign"),
    ("campaign gpu server pro", "campaign"),
    ("campaign product/gpu-server-pro", "campaign"),
    ("campaign h200 vs h100", "campaign"),
    ("campaign docs api gpu", "campaign"),
    ("campaign case study viettel ai", "campaign"),
    ("SEO GG_Search_H100_Price traffic", "url"),
    ("SEO Remarketing_H100_Readers performance", "url"),
    ("url GG_Search_AI_Inference", "url"),
    ("SEO Brand_DeCho_Awareness report", "url"),
    ("trang PMAX_GPU_Server_Trial", "url"),
    ("SEO Competitor_Viettel_AI report", "url"),
    ("ads /product/gpu-server-pro", "campaign"),
    ("campaign /resources/gpu-benchmark", "campaign"),
    ("campaign /docs/api-gpu", "campaign"),
    ("SEO competitor performance", "url"),
    ("SEO brand awareness", "url"),
    ("SEO viettel ai", "url"),
    ("campaign gpu benchmark", "campaign"),
    ("campaign api gpu", "campaign"),
    ("SEO h100 readers", "url"),
    ("SEO decho awareness", "url"),
]


CASE_GROUPS = [
    OVERLAP_SPECIFICITY_CASES,
    NO_FILTER_NATURAL_CASES,
    EXPLICIT_CUE_NO_FILTER_CASES,
    EXPLICIT_CUE_FILTER_CASES,
    NEGATION_EXCLUSION_CASES,
    RAW_LLM_DROP_CASES,
    RAW_LLM_CANONICAL_CASES,
    CAMPAIGN_SPECIFICITY_CASES,
    MULTI_KEYWORD_CASES,
    SCOPE_SAFETY_CASES,
]
ROUND3_SUBCASE_COUNT = sum(len(group) for group in CASE_GROUPS)


class IntentFilterHardeningRound3Test(unittest.TestCase):
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

    def test_round3_subcase_budget(self):
        self.assertGreaterEqual(ROUND3_SUBCASE_COUNT, 220)
        self.assertLessEqual(ROUND3_SUBCASE_COUNT, 280)

    def test_overlap_specificity_prefers_most_specific_entity(self):
        for case in OVERLAP_SPECIFICITY_CASES:
            message, include = case[:2]
            exclude = case[2] if len(case) > 2 else []
            with self.subTest(message=message):
                self.assertFilter(message, include, exclude=exclude)

    def test_no_filter_natural_language_and_negated_filter_requests(self):
        for message, action, scope in NO_FILTER_NATURAL_CASES:
            with self.subTest(message=message):
                self.assertNoFilter(message, action=action, scope=scope)

    def test_explicit_cue_boundaries_and_free_terms(self):
        for message, action, scope in EXPLICIT_CUE_NO_FILTER_CASES:
            with self.subTest(message=message, cue="substring-trap"):
                self.assertNoFilter(message, action=action, scope=scope)

        for message, scope, include, exclude, match_mode in EXPLICIT_CUE_FILTER_CASES:
            with self.subTest(message=message, cue="explicit"):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_negation_and_exclusion_keep_include_and_exclude_separate(self):
        for message, scope, include, exclude, match_mode in NEGATION_EXCLUSION_CASES:
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

    def test_raw_llm_filter_spec_drops_noisy_or_malicious_terms(self):
        for message, scope, bad_terms in RAW_LLM_DROP_CASES:
            with self.subTest(message=message):
                raw = {
                    "scope": scope,
                    "include": bad_terms,
                    "exclude": list(reversed(bad_terms)),
                    "match_mode": "all",
                }
                self.assertNoFilter(
                    message,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    extra={"filter_spec": raw},
                )

    def test_raw_llm_filter_spec_canonicalizes_to_user_grounded_terms(self):
        for message, scope, raw_include, expected_include, expected_exclude in RAW_LLM_CANONICAL_CASES:
            with self.subTest(message=message):
                raw = {"scope": scope, "include": raw_include, "exclude": expected_exclude}
                self.assertFilter(
                    message,
                    expected_include,
                    exclude=expected_exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    extra={"filter_spec": raw},
                )

    def test_campaign_specificity_and_glued_free_terms(self):
        for message, expected in CAMPAIGN_SPECIFICITY_CASES:
            with self.subTest(message=message):
                self.assertFilter(message, expected, action="ads_perf", scope="campaign")

    def test_multi_keyword_parsing_uses_any_unless_user_requests_all(self):
        for message, scope, include, exclude, match_mode in MULTI_KEYWORD_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_scope_safety_does_not_cross_url_and_campaign_catalogs(self):
        for message, scope in SCOPE_SAFETY_CASES:
            with self.subTest(message=message):
                self.assertNoFilter(
                    message,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                )


if __name__ == "__main__":
    unittest.main()
