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
        "id": "url:docs",
        "label": "docs",
        "scope": "url",
        "type": "url_group",
        "aliases": ["docs", "tai lieu", "tài liệu", "document", "documentation"],
        "patterns": ["docs"],
    },
    {
        "id": "url:blog",
        "label": "blog",
        "scope": "url",
        "type": "url_group",
        "aliases": ["blog", "bai blog", "bài blog", "article", "knowledge base"],
        "patterns": ["blog"],
    },
    {
        "id": "url:case-study",
        "label": "case study",
        "scope": "url",
        "type": "url_group",
        "aliases": ["case study", "case-study", "customer story", "cau chuyen khach hang"],
        "patterns": ["case-study"],
    },
    {
        "id": "url:pricing",
        "label": "pricing",
        "scope": "url",
        "type": "url_group",
        "aliases": ["pricing", "bang gia", "bảng giá", "price page", "gia", "giá"],
        "patterns": ["pricing"],
    },
    {
        "id": "url:solutions",
        "label": "solutions",
        "scope": "url",
        "type": "url_group",
        "aliases": ["solutions", "solution", "giai phap", "giải pháp"],
        "patterns": ["solutions"],
    },
    {
        "id": "url:product-cloud-gpu-pro",
        "label": "cloud gpu pro",
        "scope": "url",
        "type": "url",
        "aliases": [
            "cloud gpu pro",
            "cloud-gpu-pro",
            "cloudgpupro",
            "CloudGPUPro",
            "product cloud gpu pro",
            "product/cloud-gpu-pro",
        ],
        "patterns": ["product/cloud-gpu-pro"],
    },
    {
        "id": "url:product-viettel-ai",
        "label": "viettel ai platform",
        "scope": "url",
        "type": "url",
        "aliases": [
            "viettel ai platform",
            "viettel ai",
            "viettelai",
            "ViettelAI",
            "product/viettel-ai-platform",
        ],
        "patterns": ["product/viettel-ai-platform"],
    },
    {
        "id": "url:product-h100-readers",
        "label": "h100 readers",
        "scope": "url",
        "type": "url",
        "aliases": ["h100 readers", "h100readers", "H100Readers", "product/h100-readers"],
        "patterns": ["product/h100-readers"],
    },
    {
        "id": "url:docs-gpu-api",
        "label": "gpu api docs",
        "scope": "url",
        "type": "url",
        "aliases": ["gpu api docs", "api gpu docs", "docs gpu api", "docs/gpu-api", "tai lieu gpu api"],
        "patterns": ["docs/gpu-api"],
    },
    {
        "id": "url:docs-benchmark",
        "label": "benchmark docs",
        "scope": "url",
        "type": "url",
        "aliases": ["benchmark docs", "docs benchmark", "docs/benchmark", "tai lieu benchmark"],
        "patterns": ["docs/benchmark"],
    },
    {
        "id": "url:blog-h100-benchmark",
        "label": "h100 benchmark",
        "scope": "url",
        "type": "url",
        "aliases": ["h100 benchmark", "h100-benchmark", "blog h100 benchmark", "blog/h100-benchmark"],
        "patterns": ["blog/h100-benchmark"],
    },
    {
        "id": "url:blog-buy-vs-rent-gpu",
        "label": "buy vs rent gpu",
        "scope": "url",
        "type": "url",
        "aliases": ["buy vs rent gpu", "mua hay thue gpu", "mua hay thuê gpu", "blog/buy-vs-rent-gpu"],
        "patterns": ["blog/buy-vs-rent-gpu"],
    },
    {
        "id": "url:pricing-cloud-gpu",
        "label": "cloud gpu pricing",
        "scope": "url",
        "type": "url",
        "aliases": ["cloud gpu pricing", "pricing cloud gpu", "bang gia cloud gpu", "pricing/cloud-gpu"],
        "patterns": ["pricing/cloud-gpu"],
    },
    {
        "id": "url:solutions-ai-inference",
        "label": "ai inference",
        "scope": "url",
        "type": "url",
        "aliases": ["ai inference", "ai-inference", "solutions ai inference", "solutions/ai-inference"],
        "patterns": ["solutions/ai-inference"],
    },
    {
        "id": "url:solutions-edge-ai",
        "label": "edge ai",
        "scope": "url",
        "type": "url",
        "aliases": ["edge ai", "edge-ai", "solutions edge ai", "solutions/edge-ai"],
        "patterns": ["solutions/edge-ai"],
    },
    {
        "id": "url:case-study-viettel-ai",
        "label": "case study viettel ai",
        "scope": "url",
        "type": "url",
        "aliases": ["case study viettel ai", "case-study/viettel-ai", "viettel ai case study"],
        "patterns": ["case-study/viettel-ai"],
    },
    {
        "id": "url:case-study-fpt-ai",
        "label": "case study fpt ai",
        "scope": "url",
        "type": "url",
        "aliases": ["case study fpt ai", "case-study/fpt-ai", "fpt ai case study"],
        "patterns": ["case-study/fpt-ai"],
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
        "aliases": ["competitor", "doi thu", "đối thủ", "fpt", "viettel", "aws"],
        "patterns": ["fpt", "viettel", "aws"],
    },
    {
        "id": "campaign:product",
        "label": "product campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["product campaign", "product ads", "san pham campaign"],
        "patterns": ["product"],
    },
    {
        "id": "campaign:gg-search-cloud-gpu-pro",
        "label": "GG_Search_Cloud_GPU_Pro",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "gg search cloud gpu pro",
            "gg_search_cloud_gpu_pro",
            "search cloud gpu pro",
            "cloud gpu pro campaign",
        ],
        "patterns": ["gg-search-cloud-gpu-pro"],
    },
    {
        "id": "campaign:brand-awareness",
        "label": "BrandAwareness",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["brandawareness", "brand awareness", "BrandAwareness", "brand_awareness"],
        "patterns": ["brand-awareness"],
    },
    {
        "id": "campaign:brand-awareness-viettel-ai",
        "label": "Brand_Awareness_ViettelAI",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "brand awareness viettel ai",
            "brand_awareness_viettelai",
            "brand awareness viettelai",
            "BrandAwarenessViettelAI",
        ],
        "patterns": ["brand-awareness-viettel-ai"],
    },
    {
        "id": "campaign:pmax-cloudgpu-pro",
        "label": "PMAX-CloudGPUPro",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["pmax cloud gpu pro", "pmax-cloudgpupro", "PMAX-CloudGPUPro", "cloudgpupro"],
        "patterns": ["pmax-cloud-gpu-pro"],
    },
    {
        "id": "campaign:h100-readers",
        "label": "Remarketing_H100Readers",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["remarketing h100 readers", "remarketing_h100readers", "h100 readers", "H100Readers"],
        "patterns": ["remarketing-h100-readers"],
    },
    {
        "id": "campaign:competitor-viettel-ai",
        "label": "Competitor_ViettelAI",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["competitor viettel ai", "competitor_viettelai", "viettel ai", "viettelai"],
        "patterns": ["competitor-viettel-ai"],
    },
    {
        "id": "campaign:seo-leads-h100",
        "label": "seoLeadsH100",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["seo leads h100", "seoleadsh100", "seoLeadsH100"],
        "patterns": ["seo-leads-h100"],
    },
]


def fake_catalog(urls=None):
    return deepcopy(FAKE_ENTITIES)


KNOWLEDGE_NO_FILTER_CASES = [
    ("giá CloudGPUPro hiện nay là bao nhiêu", "seo_query", "url"),
    ("Cloud GPU Pro bao nhiêu tiền nếu thuê ngắn hạn", "seo_query", "url"),
    ("nên mua hay thuê GPU theo bài blog buy vs rent gpu", "seo_query", "url"),
    ("mua hay thuê GPU thì cái nào hợp lý hơn", "seo_query", "url"),
    ("H100 benchmark là gì vậy", "seo_query", "url"),
    ("benchmark docs dùng để làm gì", "seo_query", "url"),
    ("tài liệu GPU API là gì", "seo_query", "url"),
    ("docs/gpu-api cần đọc phần nào để hiểu SDK", "seo_query", "url"),
    ("case study Viettel AI là khách hàng hay đối thủ", "seo_query", "url"),
    ("case-study/fpt-ai có phải đối thủ không", "seo_query", "url"),
    ("ViettelAI là đối tác hay đối thủ của DeCho", "seo_query", "url"),
    ("CloudGPUPro so với ViettelAI khác gì", "seo_query", "url"),
    ("cloud gpu pricing có nghĩa là bảng giá nào", "seo_query", "url"),
    ("pricing/cloud-gpu giá mới nhất thế nào", "seo_query", "url"),
    ("AI inference là gì cho người mới", "seo_query", "url"),
    ("Edge AI khác AI inference ở đâu", "seo_query", "url"),
    ("H100Readers là nhóm độc giả nào", "seo_query", "url"),
    ("blog/h100-benchmark giải thích benchmark ra sao", "seo_query", "url"),
    ("blog/buy-vs-rent-gpu nên kết luận mua hay thuê", "seo_query", "url"),
    ("product/viettel-ai-platform là sản phẩm gì", "seo_query", "url"),
    ("đối thủ của Cloud GPU Pro là ai", "seo_query", "url"),
    ("tài liệu benchmark có đáng tin không", "seo_query", "url"),
    ("CloudGPUPro cần tài liệu nào để sale đọc", "seo_query", "url"),
    ("so sánh mua/thuê Cloud GPU Pro cho khách enterprise", "seo_query", "url"),
    ("benchmark H100 có phải chỉ số kỹ thuật không", "seo_query", "url"),
    ("docs benchmark có phải tài liệu nội bộ không", "seo_query", "url"),
    ("case study Viettel AI bên ngoài kể gì", "seo_query", "url"),
    ("pricing page nên giải thích giá như thế nào", "seo_query", "url"),
    ("BrandAwareness là loại campaign gì", "ads_perf", "campaign"),
    ("brand awareness nghĩa là gì trong Ads", "ads_perf", "campaign"),
    ("GG_Search_Cloud_GPU_Pro là search hay display", "ads_perf", "campaign"),
    ("PMAX-CloudGPUPro nên dùng khi nào", "ads_perf", "campaign"),
    ("Remarketing_H100Readers là audience gì", "ads_perf", "campaign"),
    ("Competitor_ViettelAI có phải nhắm đối thủ không", "ads_perf", "campaign"),
    ("seoLeadsH100 là naming convention gì", "ads_perf", "campaign"),
    ("campaign đối thủ là ai trong account này", "ads_perf", "campaign"),
    ("campaign brand khác campaign product thế nào", "ads_perf", "campaign"),
    ("chiến dịch BrandAwarenessViettelAI mục tiêu là gì", "ads_perf", "campaign"),
    ("không cần filter CloudGPUPro, chỉ hỏi giá thôi", "seo_query", "url"),
    ("đừng lọc docs/gpu-api, giải thích tài liệu giúp tôi", "seo_query", "url"),
    ("không phải case-study/viettel-ai, hỏi đối thủ nói chung", "seo_query", "url"),
    ("not CloudGPUPro, just explain buy vs rent", "seo_query", "url"),
    ("không cần filter campaign BrandAwareness, giải thích khái niệm", "ads_perf", "campaign"),
    ("not campaign PMAX-CloudGPUPro, hỏi PMAX là gì", "ads_perf", "campaign"),
]

METRIC_FILTER_CASES = [
    ("CloudGPUPro traffic giảm không", "url", ["product/cloud-gpu-pro"]),
    ("Cloud GPU Pro CTR từ organic snippets thế nào", "url", ["product/cloud-gpu-pro"]),
    ("product/cloud-gpu-pro conversion tuần này", "url", ["product/cloud-gpu-pro"]),
    ("CloudGPUPro LCP mobile cao không", "url", ["product/cloud-gpu-pro"]),
    ("Cloud GPU Pro score PageSpeed bao nhiêu", "url", ["product/cloud-gpu-pro"]),
    ("CloudGPUPro ranking keyword gpu cloud", "url", ["product/cloud-gpu-pro"]),
    ("ViettelAI platform traffic ra sao", "url", ["product/viettel-ai-platform"]),
    ("product/viettel-ai-platform conversion có tụt không", "url", ["product/viettel-ai-platform"]),
    ("Viettel AI Platform LCP và CLS", "url", ["product/viettel-ai-platform"]),
    ("H100Readers users organic thế nào", "url", ["product/h100-readers"]),
    ("product/h100-readers ranking có tăng không", "url", ["product/h100-readers"]),
    ("docs/gpu-api traffic từ Google", "url", ["docs/gpu-api"]),
    ("GPU API docs conversion hỗ trợ trial không", "url", ["docs/gpu-api"]),
    ("docs benchmark score thấp ở đâu", "url", ["docs/benchmark"]),
    ("benchmark docs LCP desktop", "url", ["docs/benchmark"]),
    ("h100 benchmark traffic 30 ngày", "url", ["blog/h100-benchmark"]),
    ("blog/h100-benchmark CTR search console", "url", ["blog/h100-benchmark"]),
    ("buy vs rent gpu conversion assist", "url", ["blog/buy-vs-rent-gpu"]),
    ("blog/buy-vs-rent-gpu ranking mua thuê gpu", "url", ["blog/buy-vs-rent-gpu"]),
    ("cloud gpu pricing CPA từ landing page", "url", ["pricing/cloud-gpu"]),
    ("pricing/cloud-gpu traffic paid landing", "url", ["pricing/cloud-gpu"]),
    ("AI inference conversion rate", "url", ["solutions/ai-inference"]),
    ("solutions/ai-inference LCP có chậm không", "url", ["solutions/ai-inference"]),
    ("edge ai ranking keyword", "url", ["solutions/edge-ai"]),
    ("solutions/edge-ai traffic có tăng không", "url", ["solutions/edge-ai"]),
    ("case study Viettel AI users và conversion", "url", ["case-study/viettel-ai"]),
    ("case-study/fpt-ai CTR organic", "url", ["case-study/fpt-ai"]),
    ("case study fpt ai LCP", "url", ["case-study/fpt-ai"]),
    ("GG_Search_Cloud_GPU_Pro spend hôm nay", "campaign", ["gg-search-cloud-gpu-pro"]),
    ("GG_Search_Cloud_GPU_Pro CTR giảm", "campaign", ["gg-search-cloud-gpu-pro"]),
    ("campaign search cloud gpu pro CPA cao", "campaign", ["gg-search-cloud-gpu-pro"]),
    ("BrandAwareness reach và frequency", "campaign", ["brand-awareness"]),
    ("brand_awareness conversion có thấp không", "campaign", ["brand-awareness"]),
    ("BrandAwarenessViettelAI spend", "campaign", ["brand-awareness-viettel-ai"]),
    ("Brand_Awareness_ViettelAI CTR", "campaign", ["brand-awareness-viettel-ai"]),
    ("PMAX-CloudGPUPro CPA", "campaign", ["pmax-cloud-gpu-pro"]),
    ("pmax cloud gpu pro conversion value", "campaign", ["pmax-cloud-gpu-pro"]),
    ("Remarketing_H100Readers CTR", "campaign", ["remarketing-h100-readers"]),
    ("H100Readers campaign spend", "campaign", ["remarketing-h100-readers"]),
    ("Competitor_ViettelAI CPA", "campaign", ["competitor-viettel-ai"]),
    ("viettelai campaign ranking auction insights", "campaign", ["competitor-viettel-ai"]),
    ("seoLeadsH100 leads và spend", "campaign", ["seo-leads-h100"]),
    ("campaign seo leads h100 CTR", "campaign", ["seo-leads-h100"]),
]

MULTI_ENTITY_CASES = [
    ("CloudGPUPro, docs/gpu-api traffic", "url", ["product/cloud-gpu-pro", "docs/gpu-api"], [], "any"),
    ("CloudGPUPro và ViettelAI platform conversion", "url", ["product/cloud-gpu-pro", "product/viettel-ai-platform"], [], "any"),
    ("CloudGPUPro hoặc H100Readers ranking", "url", ["product/cloud-gpu-pro", "product/h100-readers"], [], "any"),
    ("CloudGPUPro / docs/gpu-api / pricing/cloud-gpu", "url", ["product/cloud-gpu-pro", "docs/gpu-api", "pricing/cloud-gpu"], [], "any"),
    ("(CloudGPUPro) và (AI inference) LCP", "url", ["product/cloud-gpu-pro", "solutions/ai-inference"], [], "any"),
    ('"Cloud GPU Pro", "Edge AI" score', "url", ["product/cloud-gpu-pro", "solutions/edge-ai"], [], "any"),
    ("case-study/viettel-ai và case-study/fpt-ai traffic", "url", ["case-study/viettel-ai", "case-study/fpt-ai"], [], "any"),
    ("blog/h100-benchmark, blog/buy-vs-rent-gpu CTR", "url", ["blog/h100-benchmark", "blog/buy-vs-rent-gpu"], [], "any"),
    ("docs/gpu-api hoặc docs/benchmark users", "url", ["docs/gpu-api", "docs/benchmark"], [], "any"),
    ("pricing/cloud-gpu, solutions/ai-inference conversion", "url", ["pricing/cloud-gpu", "solutions/ai-inference"], [], "any"),
    ("CloudGPUPro trừ pricing/cloud-gpu", "url", ["product/cloud-gpu-pro"], ["pricing/cloud-gpu"], "any"),
    ("CloudGPUPro ngoại trừ docs/gpu-api", "url", ["product/cloud-gpu-pro"], ["docs/gpu-api"], "any"),
    ("CloudGPUPro except H100Readers", "url", ["product/cloud-gpu-pro"], ["product/h100-readers"], "any"),
    ("CloudGPUPro exclude ViettelAI platform", "url", ["product/cloud-gpu-pro"], ["product/viettel-ai-platform"], "any"),
    ("CloudGPUPro không gồm blog/h100-benchmark", "url", ["product/cloud-gpu-pro"], ["blog/h100-benchmark"], "any"),
    ('"AI inference" trừ "Edge AI"', "url", ["solutions/ai-inference"], ["solutions/edge-ai"], "any"),
    ("case study Viettel AI, case study FPT AI ngoại trừ docs benchmark", "url", ["case-study/viettel-ai", "case-study/fpt-ai"], ["docs/benchmark"], "any"),
    ("url /product/cloud-gpu-pro, /docs/gpu-api trừ /pricing/cloud-gpu", "url", ["product/cloud-gpu-pro", "docs/gpu-api"], ["pricing/cloud-gpu"], "any"),
    ("GG_Search_Cloud_GPU_Pro, BrandAwareness spend", "campaign", ["gg-search-cloud-gpu-pro", "brand-awareness"], [], "any"),
    ("GG_Search_Cloud_GPU_Pro và PMAX-CloudGPUPro CPA", "campaign", ["gg-search-cloud-gpu-pro", "pmax-cloud-gpu-pro"], [], "any"),
    ("BrandAwareness hoặc H100Readers CTR", "campaign", ["brand-awareness", "remarketing-h100-readers"], [], "any"),
    ("Competitor_ViettelAI / seoLeadsH100", "campaign", ["competitor-viettel-ai", "seo-leads-h100"], [], "any"),
    ('campaign "BrandAwarenessViettelAI" và "PMAX-CloudGPUPro"', "campaign", ["brand-awareness-viettel-ai", "pmax-cloud-gpu-pro"], [], "any"),
    ("GG_Search_Cloud_GPU_Pro trừ PMAX-CloudGPUPro", "campaign", ["gg-search-cloud-gpu-pro"], ["pmax-cloud-gpu-pro"], "any"),
    ("BrandAwareness ngoại trừ BrandAwarenessViettelAI", "campaign", ["brand-awareness"], ["brand-awareness-viettel-ai"], "any"),
    ("H100Readers except Competitor_ViettelAI", "campaign", ["remarketing-h100-readers"], ["competitor-viettel-ai"], "any"),
    ("seoLeadsH100 exclude BrandAwareness", "campaign", ["seo-leads-h100"], ["brand-awareness"], "any"),
    ("BrandAwareness không gồm product campaign", "campaign", ["brand-awareness"], ["product"], "any"),
    ("campaign GG_Search_Cloud_GPU_Pro, PMAX-CloudGPUPro exclude draft", "campaign", ["gg-search-cloud-gpu-pro", "pmax-cloud-gpu-pro"], ["draft"], "any"),
]

MATCH_MODE_CASES = [
    ("url phải chứa cả cloud-gpu-pro và pricing", "url", ["cloud-gpu-pro", "pricing"], [], "all"),
    ("path match all docs/gpu-api và pricing/cloud-gpu", "url", ["docs/gpu-api", "pricing/cloud-gpu"], [], "all"),
    ("trang chứa cả CloudGPUPro và docs/gpu-api", "url", ["product/cloud-gpu-pro", "docs/gpu-api"], [], "all"),
    ("campaign phải chứa cả cloud và gpu", "campaign", ["cloud", "gpu"], [], "all"),
    ("campaign match all brand và awareness", "campaign", ["brand", "awareness"], [], "all"),
    ("url chứa capacity và CloudGPUPro", "url", ["capacity", "product/cloud-gpu-pro"], [], "any"),
    ("url chứa case-study/viettel-ai và docs/gpu-api", "url", ["case-study/viettel-ai", "docs/gpu-api"], [], "any"),
    ("url catalog CloudGPUPro và docs/gpu-api", "url", ["catalog", "product/cloud-gpu-pro", "docs/gpu-api"], [], "any"),
    ("campaign chứa capacity và BrandAwareness", "campaign", ["capacity", "brand-awareness"], [], "any"),
    ("campaign case BrandAwareness và H100Readers", "campaign", ["case", "brand-awareness", "remarketing-h100-readers"], [], "any"),
    ("campaign catalog BrandAwareness và PMAX-CloudGPUPro", "campaign", ["catalog", "brand-awareness", "pmax-cloud-gpu-pro"], [], "any"),
    ("url CloudGPUPro và docs/gpu-api", "url", ["product/cloud-gpu-pro", "docs/gpu-api"], [], "any"),
    ("campaign BrandAwareness và H100Readers", "campaign", ["brand-awareness", "remarketing-h100-readers"], [], "any"),
    ("url tất cả keyword cloud-gpu-pro và pricing", "url", ["cloud-gpu-pro", "pricing"], [], "all"),
    ("campaign tất cả keyword h100 và readers", "campaign", ["h100", "readers"], [], "all"),
]

SCOPE_SAFETY_CASES = [
    ("ads product/cloud-gpu-pro traffic", "campaign"),
    ("campaign docs/gpu-api CPA", "campaign"),
    ("campaign pricing/cloud-gpu spend", "campaign"),
    ("campaign blog/h100-benchmark CTR", "campaign"),
    ("campaign case-study/viettel-ai CPA", "campaign"),
    ("ads solutions/ai-inference performance", "campaign"),
    ("SEO GG_Search_Cloud_GPU_Pro traffic", "url"),
    ("SEO PMAX-CloudGPUPro traffic", "url"),
    ("url BrandAwareness report", "url"),
    ("trang Remarketing_H100Readers traffic", "url"),
    ("SEO Competitor_ViettelAI ranking", "url"),
    ("SEO seoLeadsH100 traffic", "url"),
    ("ads CloudGPUPro performance but not campaign name", "campaign"),
    ("SEO brand awareness campaign score", "url"),
    ("SEO competitor campaign traffic", "url"),
    ("ads docs benchmark không phải campaign", "campaign"),
    ("campaign GPU API docs chỉ là URL term", "campaign"),
    ("SEO decho brand CPA", "url"),
]

SUBSTRING_TRAP_NO_FILTER_CASES = [
    ("urlencoded CloudGPUPro note", "seo_query", "url"),
    ("preurl docs/gpu-api note", "seo_query", "url"),
    ("campaigner BrandAwareness story", "ads_perf", "campaign"),
    ("campaignless PMAX-CloudGPUPro note", "ads_perf", "campaign"),
    ("landingness pricing/cloud-gpu copy", "seo_query", "url"),
    ("filterable CloudGPUPro narrative", "seo_query", "url"),
    ("pathological docs benchmark memo", "seo_query", "url"),
    ("urlencoded GG_Search_Cloud_GPU_Pro string", "seo_query", "url"),
    ("preurl BrandAwareness string", "seo_query", "url"),
    ("campaigner Competitor_ViettelAI story", "ads_perf", "campaign"),
    ("campaignless H100Readers audience idea", "ads_perf", "campaign"),
    ("landingness case-study/viettel-ai paragraph", "seo_query", "url"),
    ("filterable docs/gpu-api example", "seo_query", "url"),
    ("pathological CloudGPUPro example", "seo_query", "url"),
    ("urlencoded case-study/fpt-ai sample", "seo_query", "url"),
    ("preurl pricing/cloud-gpu sample", "seo_query", "url"),
    ("campaigner seoLeadsH100 naming", "ads_perf", "campaign"),
    ("campaignless BrandAwarenessViettelAI naming", "ads_perf", "campaign"),
]

RAW_LLM_DROP_CASES = [
    ("giá CloudGPUPro hiện nay là bao nhiêu", "url", ["cloud-gpu-pro", "price", "latest", "../secret"]),
    ("H100 benchmark là gì vậy", "url", ["h100-benchmark", "benchmark", "url"]),
    ("tài liệu GPU API là gì", "url", ["docs/gpu-api", "api", "docs"]),
    ("case study Viettel AI là khách hàng hay đối thủ", "url", ["case-study/viettel-ai", "viettel"]),
    ("CloudGPUPro so với ViettelAI khác gì", "url", ["cloudgpupro", "viettelai"]),
    ("pricing/cloud-gpu giá mới nhất thế nào", "url", ["pricing/cloud-gpu", "pricing"]),
    ("AI inference là gì cho người mới", "url", ["solutions/ai-inference", "ai"]),
    ("blog/buy-vs-rent-gpu nên kết luận mua hay thuê", "url", ["buy-vs-rent-gpu", "rent"]),
    ("đừng lọc docs/gpu-api, giải thích tài liệu giúp tôi", "url", ["docs/gpu-api"]),
    ("not CloudGPUPro, just explain buy vs rent", "url", ["cloudgpupro"]),
    ("BrandAwareness là loại campaign gì", "campaign", ["brand-awareness", "campaign"]),
    ("PMAX-CloudGPUPro nên dùng khi nào", "campaign", ["pmax-cloud-gpu-pro"]),
    ("Remarketing_H100Readers là audience gì", "campaign", ["remarketing-h100-readers"]),
    ("Competitor_ViettelAI có phải nhắm đối thủ không", "campaign", ["competitor-viettel-ai"]),
    ("campaigner BrandAwareness story", "campaign", ["brand-awareness"]),
    ("campaignless PMAX-CloudGPUPro note", "campaign", ["pmax-cloud-gpu-pro"]),
    ("urlencoded GG_Search_Cloud_GPU_Pro string", "url", ["gg-search-cloud-gpu-pro"]),
    ("pathological docs benchmark memo", "url", ["docs/benchmark"]),
]

RAW_LLM_CANONICAL_CASES = [
    ("CloudGPUPro traffic giảm không", "url", ["cloud gpu pro", "cloud-gpu-pro", "traffic"], ["product/cloud-gpu-pro"], []),
    ("product/cloud-gpu-pro conversion tuần này", "url", ["product/cloud-gpu-pro", "conversion"], ["product/cloud-gpu-pro"], []),
    ("ViettelAI platform traffic ra sao", "url", ["viettelai", "platform"], ["product/viettel-ai-platform"], []),
    ("docs/gpu-api traffic từ Google", "url", ["docs/gpu-api", "gpu api"], ["docs/gpu-api"], []),
    ("benchmark docs LCP desktop", "url", ["benchmark docs", "docs/benchmark"], ["docs/benchmark"], []),
    ("blog/h100-benchmark CTR search console", "url", ["blog/h100-benchmark", "h100"], ["blog/h100-benchmark"], []),
    ("pricing/cloud-gpu traffic paid landing", "url", ["pricing/cloud-gpu", "paid"], ["pricing/cloud-gpu"], []),
    ("solutions/ai-inference LCP có chậm không", "url", ["solutions/ai-inference"], ["solutions/ai-inference"], []),
    ("case-study/fpt-ai CTR organic", "url", ["case-study/fpt-ai", "fpt"], ["case-study/fpt-ai"], []),
    ("CloudGPUPro trừ pricing/cloud-gpu", "url", ["cloudgpupro", "pricing/cloud-gpu"], ["product/cloud-gpu-pro"], ["pricing/cloud-gpu"]),
    ("GG_Search_Cloud_GPU_Pro spend hôm nay", "campaign", ["gg-search-cloud-gpu-pro", "spend"], ["gg-search-cloud-gpu-pro"], []),
    ("BrandAwareness reach và frequency", "campaign", ["brandawareness", "brand awareness"], ["brand-awareness"], []),
    ("BrandAwarenessViettelAI spend", "campaign", ["brandawarenessviettelai"], ["brand-awareness-viettel-ai"], []),
    ("PMAX-CloudGPUPro CPA", "campaign", ["pmax-cloudgpupro", "cpa"], ["pmax-cloud-gpu-pro"], []),
    ("Remarketing_H100Readers CTR", "campaign", ["h100readers", "ctr"], ["remarketing-h100-readers"], []),
    ("Competitor_ViettelAI CPA", "campaign", ["competitor_viettelai", "viettel"], ["competitor-viettel-ai"], []),
    ("seoLeadsH100 leads và spend", "campaign", ["seoleadsh100", "spend"], ["seo-leads-h100"], []),
    ("GG_Search_Cloud_GPU_Pro trừ PMAX-CloudGPUPro", "campaign", ["gg-search-cloud-gpu-pro"], ["gg-search-cloud-gpu-pro"], ["pmax-cloud-gpu-pro"]),
]

NEGATION_SUPPRESSION_CASES = [
    ("không phải CloudGPUPro traffic, hỏi tổng quan thôi", "seo_query", "url"),
    ("not CloudGPUPro traffic, explain generally", "seo_query", "url"),
    ("đừng lọc CloudGPUPro dù có nói traffic", "seo_query", "url"),
    ("không cần filter docs/gpu-api, chỉ hỏi tài liệu", "seo_query", "url"),
    ("không cần lọc pricing/cloud-gpu, nói về giá chung", "seo_query", "url"),
    ("đừng filter case-study/viettel-ai, hỏi đối thủ là ai", "seo_query", "url"),
    ("không phải H100Readers, hỏi nhóm độc giả nói chung", "seo_query", "url"),
    ("not blog/h100-benchmark, just define benchmark", "seo_query", "url"),
    ("không phải solutions/ai-inference, hỏi AI inference là gì", "seo_query", "url"),
    ("đừng lọc Edge AI, so sánh khái niệm thôi", "seo_query", "url"),
    ("không cần filter campaign BrandAwareness dù hỏi CTR là gì", "ads_perf", "campaign"),
    ("not campaign GG_Search_Cloud_GPU_Pro, explain search campaign", "ads_perf", "campaign"),
    ("đừng lọc PMAX-CloudGPUPro, hỏi PMAX dùng khi nào", "ads_perf", "campaign"),
    ("không phải Remarketing_H100Readers, hỏi remarketing là gì", "ads_perf", "campaign"),
    ("not Competitor_ViettelAI, hỏi competitor campaign là gì", "ads_perf", "campaign"),
    ("không cần filter seoLeadsH100, nói về lead gen thôi", "ads_perf", "campaign"),
    ("đừng lọc BrandAwarenessViettelAI, hỏi brand awareness", "ads_perf", "campaign"),
    ("không phải product campaign, hỏi cấu trúc account", "ads_perf", "campaign"),
    ("not brand campaign, just compare brand vs product", "ads_perf", "campaign"),
    ("đừng filter campaign đối thủ, hỏi đối thủ là ai", "ads_perf", "campaign"),
    ("không cần lọc URL CloudGPUProLite, hỏi tên đó có ổn không", "seo_query", "url"),
    ("not campaign CloudGPUProLite, naming review only", "ads_perf", "campaign"),
]

TRUE_EXCLUSION_CASES = [
    ("CloudGPUPro trừ docs/gpu-api traffic", "url", ["product/cloud-gpu-pro"], ["docs/gpu-api"], "any"),
    ("CloudGPUPro ngoại trừ pricing/cloud-gpu conversion", "url", ["product/cloud-gpu-pro"], ["pricing/cloud-gpu"], "any"),
    ("CloudGPUPro không gồm H100Readers users", "url", ["product/cloud-gpu-pro"], ["product/h100-readers"], "any"),
    ("ViettelAI platform except case-study/viettel-ai", "url", ["product/viettel-ai-platform"], ["case-study/viettel-ai"], "any"),
    ("docs/gpu-api exclude docs/benchmark", "url", ["docs/gpu-api"], ["docs/benchmark"], "any"),
    ("blog/h100-benchmark trừ blog/buy-vs-rent-gpu", "url", ["blog/h100-benchmark"], ["blog/buy-vs-rent-gpu"], "any"),
    ("solutions/ai-inference ngoại trừ solutions/edge-ai", "url", ["solutions/ai-inference"], ["solutions/edge-ai"], "any"),
    ("case-study/viettel-ai không gồm case-study/fpt-ai", "url", ["case-study/viettel-ai"], ["case-study/fpt-ai"], "any"),
    ("GG_Search_Cloud_GPU_Pro trừ BrandAwareness spend", "campaign", ["gg-search-cloud-gpu-pro"], ["brand-awareness"], "any"),
    ("PMAX-CloudGPUPro ngoại trừ GG_Search_Cloud_GPU_Pro CPA", "campaign", ["pmax-cloud-gpu-pro"], ["gg-search-cloud-gpu-pro"], "any"),
    ("BrandAwareness không gồm H100Readers CTR", "campaign", ["brand-awareness"], ["remarketing-h100-readers"], "any"),
    ("Competitor_ViettelAI except BrandAwarenessViettelAI", "campaign", ["competitor-viettel-ai"], ["brand-awareness-viettel-ai"], "any"),
    ("seoLeadsH100 exclude PMAX-CloudGPUPro", "campaign", ["seo-leads-h100"], ["pmax-cloud-gpu-pro"], "any"),
    ("H100Readers ngoại trừ Competitor_ViettelAI", "campaign", ["remarketing-h100-readers"], ["competitor-viettel-ai"], "any"),
    ("campaign brand trừ competitor", "campaign", ["decho"], ["fpt", "viettel", "aws"], "any"),
    ("campaign product ngoại trừ brand", "campaign", ["product"], ["decho"], "any"),
    ("url phải chứa cả CloudGPUPro và docs/gpu-api trừ pricing/cloud-gpu", "url", ["product/cloud-gpu-pro", "docs/gpu-api"], ["pricing/cloud-gpu"], "all"),
    ("campaign match all cloud và gpu exclude draft", "campaign", ["cloud", "gpu"], ["draft"], "all"),
]

GLUED_CAMEL_FREE_TERM_CASES = [
    ("CloudGPUPro traffic", "url", ["product/cloud-gpu-pro"], [], "any"),
    ("ViettelAI platform conversion", "url", ["product/viettel-ai-platform"], [], "any"),
    ("H100Readers ranking", "url", ["product/h100-readers"], [], "any"),
    ("BrandAwareness CTR", "campaign", ["brand-awareness"], [], "any"),
    ("BrandAwarenessViettelAI spend", "campaign", ["brand-awareness-viettel-ai"], [], "any"),
    ("PMAX-CloudGPUPro conversion", "campaign", ["pmax-cloud-gpu-pro"], [], "any"),
    ("Remarketing_H100Readers CPA", "campaign", ["remarketing-h100-readers"], [], "any"),
    ("Competitor_ViettelAI CPA", "campaign", ["competitor-viettel-ai"], [], "any"),
    ("seoLeadsH100 spend", "campaign", ["seo-leads-h100"], [], "any"),
    ("url chứa CloudGPUProLite", "url", ["cloud-gpupro-lite"], [], "any"),
    ("url chứa ViettelAILab", "url", ["viettel-ailab"], [], "any"),
    ("url chứa H100ReadersPlus", "url", ["h100-readers-plus"], [], "any"),
    ("url chứa /labs/CloudGPUPro", "url", ["labs/CloudGPUPro"], [], "any"),
    ("path /docs/ViettelAIQuickstart", "url", ["docs/ViettelAIQuickstart"], [], "any"),
    ("url CloudGPUProLite và H100ReadersPlus", "url", ["cloud-gpupro-lite", "h100-readers-plus"], [], "any"),
    ("url phải chứa cả CloudGPUProLite và signup", "url", ["cloud-gpupro-lite", "signup"], [], "all"),
    ("campaign chứa CloudGPUProLite", "campaign", ["cloud-gpupro-lite"], [], "any"),
    ("campaign chứa BrandAwarenessLite", "campaign", ["brand-awareness-lite"], [], "any"),
    ("campaign chứa H100ReadersPlus", "campaign", ["h100-readers-plus"], [], "any"),
    ("campaign CloudGPUProLite và BrandAwarenessLite", "campaign", ["cloud-gpupro-lite", "brand-awareness-lite"], [], "any"),
    ("campaign phải chứa cả CloudGPUProLite và trial", "campaign", ["cloud-gpupro-lite", "trial"], [], "all"),
    ("campaign chứa ViettelAIProspecting exclude draft", "campaign", ["viettel-aiprospecting"], ["draft"], "any"),
    ("url chứa CloudGPUProLite exclude archive", "url", ["cloud-gpupro-lite"], ["archive"], "any"),
    ("campaign chứa H100ReadersPlus ngoại trừ paused", "campaign", ["h100-readers-plus"], ["paused"], "any"),
]


CASE_GROUPS = [
    KNOWLEDGE_NO_FILTER_CASES,
    METRIC_FILTER_CASES,
    MULTI_ENTITY_CASES,
    MATCH_MODE_CASES,
    SCOPE_SAFETY_CASES,
    SUBSTRING_TRAP_NO_FILTER_CASES,
    RAW_LLM_DROP_CASES,
    RAW_LLM_CANONICAL_CASES,
    NEGATION_SUPPRESSION_CASES,
    TRUE_EXCLUSION_CASES,
    GLUED_CAMEL_FREE_TERM_CASES,
]
ROUND4_SUBCASE_COUNT = sum(len(group) for group in CASE_GROUPS)


class IntentFilterHardeningRound4Test(unittest.TestCase):
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

    def test_round4_subcase_budget(self):
        self.assertGreaterEqual(ROUND4_SUBCASE_COUNT, 260)
        self.assertLessEqual(ROUND4_SUBCASE_COUNT, 340)

    def test_knowledge_and_advice_questions_do_not_become_filters(self):
        for message, action, scope in KNOWLEDGE_NO_FILTER_CASES:
            with self.subTest(message=message):
                self.assertNoFilter(message, action=action, scope=scope)

    def test_metric_and_analysis_cues_with_entities_become_filters(self):
        for message, scope, include in METRIC_FILTER_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                )

    def test_multi_entity_include_exclude_punctuation_and_connectors(self):
        for message, scope, include, exclude, match_mode in MULTI_ENTITY_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_match_mode_is_any_unless_the_user_explicitly_requests_all(self):
        for message, scope, include, exclude, match_mode in MATCH_MODE_CASES:
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_scope_safety_between_url_and_campaign_entities(self):
        for message, scope in SCOPE_SAFETY_CASES:
            with self.subTest(message=message):
                self.assertNoFilter(
                    message,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                )

    def test_substring_traps_do_not_create_filter_context(self):
        for message, action, scope in SUBSTRING_TRAP_NO_FILTER_CASES:
            with self.subTest(message=message):
                self.assertNoFilter(message, action=action, scope=scope)

    def test_raw_llm_filter_spec_drops_noisy_or_malicious_terms(self):
        for message, scope, bad_terms in RAW_LLM_DROP_CASES:
            with self.subTest(message=message):
                raw = {
                    "scope": scope,
                    "include": bad_terms,
                    "exclude": ["../etc/passwd", "drop-table", *reversed(bad_terms)],
                    "match_mode": "all",
                }
                self.assertNoFilter(
                    message,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    extra={"filter_spec": raw},
                )

    def test_raw_llm_filter_spec_canonicalizes_to_user_grounded_entities(self):
        for message, scope, raw_include, expected_include, expected_exclude in RAW_LLM_CANONICAL_CASES:
            with self.subTest(message=message):
                raw = {
                    "scope": scope,
                    "include": raw_include,
                    "exclude": expected_exclude + ["../etc/passwd", "unmentioned"],
                    "match_mode": "all",
                }
                self.assertFilter(
                    message,
                    expected_include,
                    exclude=expected_exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    extra={"filter_spec": raw},
                )

    def test_negated_filter_requests_are_suppressed_but_real_excludes_survive(self):
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

    def test_glued_camel_tokens_distinguish_entities_from_explicit_free_terms(self):
        for message, scope, include, exclude, match_mode in GLUED_CAMEL_FREE_TERM_CASES:
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
