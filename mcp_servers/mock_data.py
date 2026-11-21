"""五域女装电商高仿真 Mock 数据底座（女装成衣商品库存、订单售后、顺丰/圆通/京东物流轨迹、按日分区经营数仓、市场趋势与成衣供应链）。"""

from __future__ import annotations

import copy
from typing import Any

# ============================================================================
# 1. PIM 域：女装多品类商品与库存目录（含成衣出厂底价等后端内部字段用于验证出参裁剪）
# ============================================================================

INITIAL_SKU_CATALOG: dict[str, dict[str, Any]] = {
    "SKU-DRESS-SILK-01": {
        "sku_id": "SKU-DRESS-SILK-01",
        "tenant_ids": ["tenant_yunshang_cn"],
        "name": "法式桑蚕丝收腰茶歇连衣裙 香槟杏",
        "category": "连衣裙",
        "brand": "云裳织造",
        "retail_price": 599.0,
        "available_stock": 86,
        "warehouse": "华东杭州云裳中心仓",
        "sizes": "XS / S / M / L / XL",
        "color": "香槟杏",
        "fabric": "92% 6A级桑蚕丝 + 8% 氨纶（双层防透真丝电力纺全衬）",
        "specs": {
            "sizes": "XS / S / M / L / XL",
            "color": "香槟杏",
            "fabric": "92% 6A级桑蚕丝 + 8% 氨纶（双层防透真丝电力纺全衬）",
            "fit": "立体省道高腰A字剪裁（包容梨形身材）",
            "care": "冷水轻柔手洗 / 专业真丝干洗",
            "styling_match": "可单穿约会度假，春秋外搭精纺美丽奴羊毛小香风针织开衫",
        },
        "selling_points": [
            "6A级重磅双绉桑蚕丝亲肤垂顺自带珍珠柔光感",
            "双层全幅防透内衬 + 立体高腰省道解决浅色裙透肉与显小腹痛点",
            "侧腰隐形调节暗扣兼顾通勤优雅与周末度假穿搭",
        ],
        "rating": 4.93,
        "_internal_supplier_cost": 235.0,
        "_internal_warehouse_bin": "HZ-A01-08",
    },
    "SKU-DRESS-ACETATE-03": {
        "sku_id": "SKU-DRESS-ACETATE-03",
        "tenant_ids": ["tenant_yunshang_cn"],
        "name": "通勤赫本风三醋酸无袖A字连衣裙 曜石黑",
        "category": "连衣裙",
        "brand": "云裳织造",
        "retail_price": 459.0,
        "available_stock": 95,
        "warehouse": "华东杭州云裳中心仓",
        "sizes": "S / M / L / XL",
        "color": "曜石黑",
        "fabric": "75% 进口三醋酸 + 25% 聚酯纤维（自带铜氨丝透气里布）",
        "specs": {
            "sizes": "S / M / L / XL",
            "color": "曜石黑",
            "fabric": "75% 进口三醋酸 + 25% 聚酯纤维（自带铜氨丝透气里布）",
            "fit": "正肩收腰大摆赫本A字版型",
            "care": "30℃以下轻柔手洗悬挂阴干免熨烫",
            "styling_match": "通勤可叠穿珍珠白飘带衬衫或奶霜白羊毛针织开衫",
        },
        "selling_points": [
            "进口三醋酸骨感垂坠抗皱免打理，久坐会议不易留折痕",
            "铜氨丝吸湿排汗内衬夏日贴肤清凉不起静电",
            "加宽立体腰节视觉收窄腰线并自然遮掩假胯宽",
        ],
        "rating": 4.90,
        "_internal_supplier_cost": 169.0,
        "_internal_warehouse_bin": "HZ-A02-15",
    },
    "SKU-DRESS-CHIFFON-02": {
        "sku_id": "SKU-DRESS-CHIFFON-02",
        "tenant_ids": ["tenant_yunshang_cn"],
        "name": "轻盈碎花雪纺泡泡袖连衣裙 雾粉紫",
        "category": "连衣裙",
        "brand": "云裳织造",
        "retail_price": 239.0,
        "available_stock": 0,
        "warehouse": "华东杭州云裳中心仓",
        "sizes": "S / M / L",
        "color": "雾粉紫",
        "fabric": "100% 聚酯雪纺（单层半衬）",
        "specs": {
            "sizes": "S / M / L",
            "color": "雾粉紫",
            "fabric": "100% 聚酯雪纺（单层半衬）",
            "fit": "修身收腰泡泡袖版型",
            "care": "机洗轻柔模式",
            "styling_match": "适合春季踏青搭配浅色针织披肩",
        },
        "selling_points": [
            "春日法式碎花印花搭配方领修饰锁骨线条",
            "轻盈飘逸裙摆适合出游拍照",
        ],
        "rating": 4.52,
        "_internal_supplier_cost": 82.0,
        "_internal_warehouse_bin": "HZ-A04-02",
    },
    "SKU-SHIRT-ACETATE-01": {
        "sku_id": "SKU-SHIRT-ACETATE-01",
        "tenant_ids": ["tenant_yunshang_cn"],
        "name": "重磅三醋酸垂感飘带法式衬衫 珍珠白",
        "category": "衬衫",
        "brand": "云裳织造",
        "retail_price": 399.0,
        "available_stock": 160,
        "warehouse": "华东杭州云裳中心仓",
        "sizes": "S / M / L / XL",
        "color": "珍珠白",
        "fabric": "78% 进口三醋酸 + 22% 聚酯纤维（高密防透缎面）",
        "specs": {
            "sizes": "S / M / L / XL",
            "color": "珍珠白",
            "fabric": "78% 进口三醋酸 + 22% 聚酯纤维（高密防透缎面）",
            "fit": "微宽松落肩H型剪裁",
            "care": "轻柔手洗或套袋机洗悬挂即平",
            "styling_match": "原生适配高腰双褶垂感窄版西装阔腿裤（SKU-PANTS-WIDE-01）与微喇牛仔裤成套通勤穿搭",
        },
        "selling_points": [
            "进口重磅高密三醋酸缎面垂顺抗皱且白色不透内搭",
            "可拆卸飘带V领两穿设计无缝衔接职场干练与约会柔美",
            "胸前加密隐形防走光暗扣拒绝抬手崩开尴尬",
        ],
        "rating": 4.91,
        "_internal_supplier_cost": 148.0,
        "_internal_warehouse_bin": "HZ-B02-05",
    },
    "SKU-SHIRT-COTTON-02": {
        "sku_id": "SKU-SHIRT-COTTON-02",
        "tenant_ids": ["tenant_yunshang_cn"],
        "name": "新疆长绒棉修身牛津纺通勤衬衫 浅雾蓝",
        "category": "衬衫",
        "brand": "云裳织造",
        "retail_price": 189.0,
        "available_stock": 3,
        "warehouse": "华东嘉兴桐乡分拨仓",
        "sizes": "S / M / L",
        "color": "浅雾蓝",
        "fabric": "100% 新疆80支双股长绒棉",
        "specs": {
            "sizes": "S / M / L",
            "color": "浅雾蓝",
            "fabric": "100% 新疆80支双股长绒棉",
            "fit": "利落正肩微收腰版型",
            "care": "常规机洗低温蒸汽熨烫",
            "styling_match": "适合搭配燕麦米杏高腰阔腿西装裤或复古丹宁微喇牛仔裤",
        },
        "selling_points": [
            "80支双股新疆长绒棉挺括透气吸汗不贴身",
            "成衣水洗预缩定型工艺多次洗涤领口不易变形",
        ],
        "rating": 4.78,
        "_internal_supplier_cost": 76.0,
        "_internal_warehouse_bin": "JX-D09-01",
    },
    "SKU-PANTS-WIDE-01": {
        "sku_id": "SKU-PANTS-WIDE-01",
        "tenant_ids": ["tenant_yunshang_cn", "tenant_other_shop"],
        "name": "高腰双褶垂感窄版西装阔腿裤 燕麦米杏",
        "category": "裤装",
        "brand": "云裳织造",
        "retail_price": 359.0,
        "available_stock": 128,
        "warehouse": "华东杭州云裳中心仓",
        "sizes": "XS / S / M / L / XL（常规裤长/加长裤长）",
        "color": "燕麦米杏",
        "fabric": "68% 聚酯纤维 + 29% 粘胶纤维 + 3% 氨纶（抗起球垂感西装料）",
        "specs": {
            "sizes": "XS / S / M / L / XL（常规裤长/加长裤长）",
            "color": "燕麦米杏",
            "fabric": "68% 聚酯纤维 + 29% 粘胶纤维 + 3% 氨纶（抗起球垂感西装料）",
            "fit": "高腰立体双褶窄版直筒阔腿",
            "care": "套洗衣袋机洗自然垂挂免烫",
            "styling_match": "百搭重磅三醋酸飘带衬衫、长绒棉牛津纺衬衫及短款羊毛针织开衫",
        },
        "selling_points": [
            "高腰双捏褶立体裤型视觉拉长腿部比例且巧妙遮胯显瘦",
            "区分常规版与加长版双裤长精准适配 155cm-175cm 不同身高",
            "高密垂感西装面料抗皱不起球久坐膝盖不鼓包",
        ],
        "rating": 4.90,
        "_internal_supplier_cost": 132.0,
        "_internal_warehouse_bin": "HZ-C03-19",
    },
    "SKU-PANTS-JEANS-02": {
        "sku_id": "SKU-PANTS-JEANS-02",
        "tenant_ids": ["tenant_yunshang_cn"],
        "name": "莱赛尔天丝微喇九分牛仔裤 复古丹宁蓝",
        "category": "裤装",
        "brand": "云裳织造",
        "retail_price": 279.0,
        "available_stock": 74,
        "warehouse": "华南广州新塘牛仔仓",
        "sizes": "25 / 26 / 27 / 28 / 29",
        "color": "复古丹宁蓝",
        "fabric": "65% 棉 + 33% 莱赛尔天丝 + 2% 氨纶",
        "specs": {
            "sizes": "25 / 26 / 27 / 28 / 29",
            "color": "复古丹宁蓝",
            "fabric": "65% 棉 + 33% 莱赛尔天丝 + 2% 氨纶",
            "fit": "高腰提臀微喇修饰小腿版型",
            "care": "反面冷水单独洗涤防串色",
            "styling_match": "适合搭配法式衬衫或短款羊毛开衫打造松弛通勤风",
        },
        "selling_points": [
            "融入莱赛尔天丝云感柔滑透气告别传统硬挺牛仔束缚感",
            "微喇裤口精准修饰小腿线条视觉显腿直",
        ],
        "rating": 4.85,
        "_internal_supplier_cost": 105.0,
        "_internal_warehouse_bin": "GZ-E01-06",
    },
    "SKU-KNIT-CARDIGAN-01": {
        "sku_id": "SKU-KNIT-CARDIGAN-01",
        "tenant_ids": ["tenant_yunshang_cn"],
        "name": "精纺美丽奴羊毛小香风针织开衫 奶霜白",
        "category": "针织衫",
        "brand": "云裳织造",
        "retail_price": 429.0,
        "available_stock": 64,
        "warehouse": "华东嘉兴桐乡分拨仓",
        "sizes": "S / M / L",
        "color": "奶霜白",
        "fabric": "85% 澳洲美丽奴超细羊毛 + 15% 内蒙古山羊绒",
        "specs": {
            "sizes": "S / M / L",
            "color": "奶霜白",
            "fabric": "85% 澳洲美丽奴超细羊毛 + 15% 内蒙古山羊绒",
            "fit": "正肩合身短款圆领版型",
            "care": "专业干洗或羊毛专用中性洗涤剂平铺阴干",
            "styling_match": "外搭香槟杏桑蚕丝连衣裙或内搭珍珠白衬衫配高腰阔腿裤",
        },
        "selling_points": [
            "17.5微米澳洲超细美丽奴羊毛混纺山羊绒贴身软糯不扎肤",
            "天然深海贝母扣短款比例轻松穿出利落高腰线",
        ],
        "rating": 4.89,
        "_internal_supplier_cost": 168.0,
        "_internal_warehouse_bin": "JX-F02-11",
    },
}

SELLING_POINTS_KNOWLEDGE_MD = """# 云裳女装商品卖点知识库与导购合规话术规范

## 一、核心女装品类差异化卖点与尺码穿搭指南
1. **连衣裙系列（桑蚕丝 / 三醋酸通勤与茶歇裙）**
   - 轻奢真丝款（法式桑蚕丝收腰茶歇连衣裙 `SKU-DRESS-SILK-01`，¥599）：主打 **92% 6A级桑蚕丝** + **双层防透真丝电力纺全衬** + **立体高腰A字省道**，彻底解决浅色真丝透肉痛点，适合约会、晚宴及高品质通勤。
   - 抗皱通勤款（赫本风三醋酸无袖A字连衣裙 `SKU-DRESS-ACETATE-03`，¥459）：主打 **75% 进口三醋酸免烫垂坠** + **铜氨丝吸湿排汗内衬**，适合职场会议与差旅场景。
2. **衬衫系列（成套职场穿搭主力）**
   - 旗舰款（重磅三醋酸垂感飘带法式衬衫 `SKU-SHIRT-ACETATE-01`，¥399）：主打 **高密防透三醋酸缎面** + **可拆卸飘带两穿** + **胸前防走光隐形暗扣**，与燕麦米杏高腰阔腿西装裤（`SKU-PANTS-WIDE-01`）为店长力推成套王牌组合。
   - 基础款（新疆长绒棉修身牛津纺衬衫 `SKU-SHIRT-COTTON-02`，¥189）：主打 **80支双股新疆长绒棉** 挺括吸汗，适合日常高频洗护叠穿。
3. **裤装与针织衫系列**
   - 高腰双褶西装阔腿裤（`SKU-PANTS-WIDE-01`，¥359）：提供 **常规裤长/加长裤长** 双规格，包容梨形假胯宽，久坐膝盖不鼓包。
   - 美丽奴羊毛混纺羊绒开衫（`SKU-KNIT-CARDIGAN-01`，¥429）：软糯亲肤不扎肉，适合早晚温差外搭连衣裙。

## 二、女装导购负向合规红线
- 严禁使用“100%绝对不皱”“一穿暴瘦10斤”“永久不起球不缩水”等违反广告法的绝对化极限词；
- 推荐尺码时须客观说明版型松量与面料弹力，库存低于 5 件时明确提示“尺码库存紧张以实际锁单为准”，库存为 0 时主动引导同品类有货替代款。
"""


# ============================================================================
# 2. OMS 域：买家与商家双视角女装订单池（覆盖多租户与同租户不同买家边界）
# ============================================================================

INITIAL_ORDERS_STORE: dict[str, dict[str, Any]] = {
    "ORD-20261025-2100": {
        "order_id": "ORD-20261025-2100",
        "tenant_id": "tenant_yunshang_cn",
        "buyer_user_id": "buyer_1001",
        "status": "completed",
        "created_at": "2026-10-25",
        "sku_id": "SKU-PANTS-WIDE-01",
        "sku_name": "高腰双褶垂感窄版西装阔腿裤 燕麦米杏（M码/常规裤长）",
        "quantity": 1,
        "total_amount": 359.0,
        "buyer_name": "林晓月",
        "buyer_phone": "13812345678",
        "buyer_email": "linxiaoyue@example.com",
        "shipping_address": "广东省深圳市南山区粤海街道高新南一道18号",
        "tracking_no": "SF2026882100",
        "_internal_channel_fee": 17.95,
        "_internal_risk_score": 0.01,
    },
    "ORD-20261118-2101": {
        "order_id": "ORD-20261118-2101",
        "tenant_id": "tenant_yunshang_cn",
        "buyer_user_id": "buyer_1001",
        "status": "paid",
        "created_at": "2026-11-18",
        "sku_id": "SKU-SHIRT-ACETATE-01",
        "sku_name": "重磅三醋酸垂感飘带法式衬衫 珍珠白（M码）",
        "quantity": 1,
        "total_amount": 399.0,
        "buyer_name": "林晓月",
        "buyer_phone": "13812345678",
        "buyer_email": "linxiaoyue@example.com",
        "shipping_address": "广东省深圳市南山区粤海街道高新南一道18号",
        "tracking_no": None,
        "_internal_channel_fee": 19.95,
        "_internal_risk_score": 0.02,
    },
    "ORD-20261115-2102": {
        "order_id": "ORD-20261115-2102",
        "tenant_id": "tenant_yunshang_cn",
        "buyer_user_id": "buyer_1001",
        "status": "shipped",
        "created_at": "2026-11-15",
        "sku_id": "SKU-DRESS-SILK-01",
        "sku_name": "法式桑蚕丝收腰茶歇连衣裙 香槟杏（S码）",
        "quantity": 1,
        "total_amount": 599.0,
        "buyer_name": "林晓月",
        "buyer_phone": "13812345678",
        "buyer_email": "linxiaoyue@example.com",
        "shipping_address": "广东省深圳市南山区粤海街道高新南一道18号",
        "tracking_no": "SF2026882102",
        "_internal_channel_fee": 29.95,
        "_internal_risk_score": 0.04,
    },
    "ORD-20261110-2103": {
        "order_id": "ORD-20261110-2103",
        "tenant_id": "tenant_yunshang_cn",
        "buyer_user_id": "buyer_1002",
        "status": "completed",
        "created_at": "2026-11-10",
        "sku_id": "SKU-PANTS-WIDE-01",
        "sku_name": "高腰双褶垂感窄版西装阔腿裤 燕麦米杏（L码/加长裤长）",
        "quantity": 2,
        "total_amount": 718.0,
        "buyer_name": "周明姝",
        "buyer_phone": "13987654321",
        "buyer_email": "zhoums@example.com",
        "shipping_address": "浙江省杭州市余杭区文一西路969号欧美金融城",
        "tracking_no": "YT8833002103",
        "_internal_channel_fee": 35.90,
        "_internal_risk_score": 0.01,
    },
    "ORD-20261101-2104": {
        "order_id": "ORD-20261101-2104",
        "tenant_id": "tenant_yunshang_cn",
        "buyer_user_id": "buyer_1001",
        "status": "refunded",
        "created_at": "2026-11-01",
        "sku_id": "SKU-KNIT-CARDIGAN-01",
        "sku_name": "精纺美丽奴羊毛小香风针织开衫 奶霜白（S码）",
        "quantity": 1,
        "total_amount": 429.0,
        "buyer_name": "林晓月",
        "buyer_phone": "13812345678",
        "buyer_email": "linxiaoyue@example.com",
        "shipping_address": "广东省深圳市南山区粤海街道高新南一道18号",
        "tracking_no": None,
        "_internal_channel_fee": 21.45,
        "_internal_risk_score": 0.08,
    },
    "ORD-20261119-3001": {
        "order_id": "ORD-20261119-3001",
        "tenant_id": "tenant_other_shop",
        "buyer_user_id": "buyer_2001",
        "status": "paid",
        "created_at": "2026-11-19",
        "sku_id": "SKU-PANTS-WIDE-01",
        "sku_name": "高腰双褶垂感窄版西装阔腿裤 燕麦米杏（M码/常规裤长）",
        "quantity": 1,
        "total_amount": 359.0,
        "buyer_name": "陈海琳",
        "buyer_phone": "13711112222",
        "buyer_email": "chenhl@example.com",
        "shipping_address": "上海市徐汇区漕河泾新兴技术开发区桂平路88号",
        "tracking_no": "JD6655003001",
        "_internal_channel_fee": 17.95,
        "_internal_risk_score": 0.03,
    },
}

ORDER_STATE_MACHINE_MD = """# 女装订单与售后状态机规范（OMS）

## 一、标准状态流转
- `pending`（待支付） -> `paid`（已支付待成衣质检出库） -> `shipped`（已发货运输中） -> `completed`（已签收完结）
- 逆向售后流转：`paid` / `shipped` / `completed` -> `refunded`（已原路退款关闭）

## 二、动作准入矩阵
1. **修改收货地址（`update_address`，风险等级：`medium`）**
   - 仅允许 `status in {"pending", "paid"}`（杭州/嘉兴服饰仓未打单揽收前）执行；
   - 一旦进入 `shipped`、`completed` 或 `refunded`，严禁直接改地址，返回 `ORDER_STATUS_DISALLOWS_ADDRESS_UPDATE` 业务错误。
2. **创建退款单（`create_refund`，风险等级：`high`，强制 HITL 人工审批）**
   - 仅允许 `status in {"paid", "shipped", "completed"}` 且 `refund_amount <= total_amount`；
   - `refunded` 状态禁止重复退款，返回 `ORDER_ALREADY_REFUNDED`。
"""


# ============================================================================
# 3. Logistics 域：女装成衣包裹跨承运商物流轨迹数据
# ============================================================================

INITIAL_LOGISTICS_PACKAGES: dict[str, dict[str, Any]] = {
    "SF2026882102": {
        "tracking_no": "SF2026882102",
        "order_id": "ORD-20261115-2102",
        "tenant_id": "tenant_yunshang_cn",
        "buyer_user_id": "buyer_1001",
        "carrier": "顺丰速运",
        "delivery_status": "in_transit",
        "estimated_delivery": "2026-11-21 18:00:00",
        "receiver_name": "林晓月",
        "receiver_phone": "13812345678",
        "receiver_address": "广东省深圳市南山区粤海街道高新南一道18号",
        "checkpoints": [
            {
                "timestamp": "2026-11-15 16:20:00",
                "location": "杭州市萧山云裳恒温女装仓",
                "status": "真丝成衣已完成蒸汽整烫与防尘挂装打包，顺丰速运已揽收",
            },
            {
                "timestamp": "2026-11-16 02:15:00",
                "location": "杭州萧山航空集散中心",
                "status": "快件已完成航空分拣装机，正发往深圳宝安华南枢纽",
            },
            {
                "timestamp": "2026-11-20 08:40:00",
                "location": "深圳南山科技园营业部",
                "status": "派件员（工号 SF9021，联系电话 13699887766）正在派送途中",
            },
        ],
        "_internal_carrier_settlement_cost": 13.5,
    },
    "YT8833002103": {
        "tracking_no": "YT8833002103",
        "order_id": "ORD-20261110-2103",
        "tenant_id": "tenant_yunshang_cn",
        "buyer_user_id": "buyer_1002",
        "carrier": "圆通速递",
        "delivery_status": "delivered",
        "estimated_delivery": "2026-11-12 15:00:00",
        "receiver_name": "周明姝",
        "receiver_phone": "13987654321",
        "receiver_address": "浙江省杭州市余杭区文一西路969号欧美金融城",
        "checkpoints": [
            {
                "timestamp": "2026-11-10 19:00:00",
                "location": "杭州市萧山云裳恒温女装仓",
                "status": "商家已发货，圆通速递已揽收包裹",
            },
            {
                "timestamp": "2026-11-12 14:12:00",
                "location": "杭州未来科技城菜鸟驿站",
                "status": "包裹已签收（驿站代收凭取件码签收）",
            },
        ],
        "_internal_carrier_settlement_cost": 5.2,
    },
    "JD6655003001": {
        "tracking_no": "JD6655003001",
        "order_id": "ORD-20261119-3001",
        "tenant_id": "tenant_other_shop",
        "buyer_user_id": "buyer_2001",
        "carrier": "京东物流",
        "delivery_status": "in_transit",
        "estimated_delivery": "2026-11-21 12:00:00",
        "receiver_name": "陈海琳",
        "receiver_phone": "13711112222",
        "receiver_address": "上海市徐汇区漕河泾新兴技术开发区桂平路88号",
        "checkpoints": [
            {
                "timestamp": "2026-11-19 21:10:00",
                "location": "上海亚洲一号服饰智能仓",
                "status": "京东快递已完成打包出库",
            }
        ],
        "_internal_carrier_settlement_cost": 8.5,
    },
}


# ============================================================================
# 4. BI 域：按日分区（dt）女装经营分析明细数据（覆盖连衣裙高退货归因与租户物理隔离）
# ============================================================================

BI_DAILY_METRICS_ROWS: list[dict[str, Any]] = [
    # tenant_yunshang_cn - 衬衫（健康高复购通勤主力品类）
    {
        "dt": "2026-11-18",
        "tenant_id": "tenant_yunshang_cn",
        "category": "衬衫",
        "sku_id": "SKU-SHIRT-ACETATE-01",
        "price_band": "300-499",
        "uv": 6400,
        "paid_orders": 384,
        "gmv": 153216.0,
        "refund_orders": 25,
        "return_rate": 0.0651,
        "conversion_rate": 0.0600,
        "gross_margin_rate": 0.629,
    },
    {
        "dt": "2026-11-19",
        "tenant_id": "tenant_yunshang_cn",
        "category": "衬衫",
        "sku_id": "SKU-SHIRT-ACETATE-01",
        "price_band": "300-499",
        "uv": 6800,
        "paid_orders": 408,
        "gmv": 162792.0,
        "refund_orders": 27,
        "return_rate": 0.0662,
        "conversion_rate": 0.0600,
        "gross_margin_rate": 0.629,
    },
    {
        "dt": "2026-11-20",
        "tenant_id": "tenant_yunshang_cn",
        "category": "衬衫",
        "sku_id": "SKU-SHIRT-COTTON-02",
        "price_band": "100-199",
        "uv": 4800,
        "paid_orders": 240,
        "gmv": 45360.0,
        "refund_orders": 24,
        "return_rate": 0.1000,
        "conversion_rate": 0.0500,
        "gross_margin_rate": 0.598,
    },
    # tenant_yunshang_cn - 连衣裙（200-299 价格带高退货异常品类 + 329~499 轻奢通勤带缺口）
    {
        "dt": "2026-11-18",
        "tenant_id": "tenant_yunshang_cn",
        "category": "连衣裙",
        "sku_id": "SKU-DRESS-CHIFFON-02",
        "price_band": "200-299",
        "uv": 5200,
        "paid_orders": 260,
        "gmv": 62140.0,
        "refund_orders": 88,
        "return_rate": 0.3385,
        "conversion_rate": 0.0500,
        "gross_margin_rate": 0.585,
    },
    {
        "dt": "2026-11-19",
        "tenant_id": "tenant_yunshang_cn",
        "category": "连衣裙",
        "sku_id": "SKU-DRESS-CHIFFON-02",
        "price_band": "200-299",
        "uv": 5500,
        "paid_orders": 275,
        "gmv": 65725.0,
        "refund_orders": 96,
        "return_rate": 0.3491,
        "conversion_rate": 0.0500,
        "gross_margin_rate": 0.585,
    },
    {
        "dt": "2026-11-20",
        "tenant_id": "tenant_yunshang_cn",
        "category": "连衣裙",
        "sku_id": "SKU-DRESS-SILK-01",
        "price_band": "500-699",
        "uv": 4400,
        "paid_orders": 308,
        "gmv": 184492.0,
        "refund_orders": 29,
        "return_rate": 0.0942,
        "conversion_rate": 0.0700,
        "gross_margin_rate": 0.608,
    },
    # tenant_yunshang_cn - 裤装（稳健引流下装品类）
    {
        "dt": "2026-11-20",
        "tenant_id": "tenant_yunshang_cn",
        "category": "裤装",
        "sku_id": "SKU-PANTS-WIDE-01",
        "price_band": "300-399",
        "uv": 5100,
        "paid_orders": 357,
        "gmv": 128163.0,
        "refund_orders": 28,
        "return_rate": 0.0784,
        "conversion_rate": 0.0700,
        "gross_margin_rate": 0.632,
    },
    # 另一租户数据（用于验证 BI SQL 租户行级物理隔离，tenant_yunshang_cn 严禁查出此行）
    {
        "dt": "2026-11-20",
        "tenant_id": "tenant_other_shop",
        "category": "连衣裙",
        "sku_id": "SKU-OTHER-DRESS-999",
        "price_band": "300-499",
        "uv": 99999,
        "paid_orders": 8888,
        "gmv": 9999999.0,
        "refund_orders": 100,
        "return_rate": 0.0112,
        "conversion_rate": 0.0888,
        "gross_margin_rate": 0.680,
    },
]

BI_RETURN_REASON_ROWS: list[dict[str, Any]] = [
    {
        "dt": "2026-11-19",
        "tenant_id": "tenant_yunshang_cn",
        "category": "连衣裙",
        "sku_id": "SKU-DRESS-CHIFFON-02",
        "return_reason": "浅色雪纺单层内衬偏透易走光",
        "return_count": 82,
        "refund_amount": 19598.0,
    },
    {
        "dt": "2026-11-19",
        "tenant_id": "tenant_yunshang_cn",
        "category": "连衣裙",
        "sku_id": "SKU-DRESS-CHIFFON-02",
        "return_reason": "腰臀尺码偏小梨形身材拉链紧绷",
        "return_count": 64,
        "refund_amount": 15296.0,
    },
    {
        "dt": "2026-11-20",
        "tenant_id": "tenant_yunshang_cn",
        "category": "连衣裙",
        "sku_id": "SKU-DRESS-CHIFFON-02",
        "return_reason": "化纤雪纺静电贴腿闷汗不透气",
        "return_count": 38,
        "refund_amount": 9082.0,
    },
    {
        "dt": "2026-11-20",
        "tenant_id": "tenant_yunshang_cn",
        "category": "衬衫",
        "sku_id": "SKU-SHIRT-COTTON-02",
        "return_reason": "正肩版型偏修身大胸围穿着略紧",
        "return_count": 24,
        "refund_amount": 4536.0,
    },
    {
        "dt": "2026-11-20",
        "tenant_id": "tenant_other_shop",
        "category": "连衣裙",
        "sku_id": "SKU-OTHER-DRESS-999",
        "return_reason": "跨租户机密退货原因",
        "return_count": 500,
        "refund_amount": 250000.0,
    },
]

BI_METRIC_DICTIONARY_MD = """# 女装经营分析数仓指标口径字典（BI）

## 一、强制查询安全红线
1. **严格只读**：`run_readonly_sql` 仅允许单条 `SELECT` / `WITH ... SELECT` 语句，严禁任何 `INSERT` / `UPDATE` / `DELETE` / `DROP` / `ALTER` / `PRAGMA` 或分号多语句绕过。
2. **强制时间分区裁剪**：所有明细表均以 `dt`（`YYYY-MM-DD`）作为天级分区列，SQL `WHERE` 子句必须显式包含 `dt` 分区过滤条件（如 `dt >= '2026-11-18'` 或 `dt BETWEEN '2026-11-18' AND '2026-11-20'`），禁止无分区全表扫描。

## 二、女装核心经营指标定义
- `gmv`：支付成功女装成衣成交总额（元），不含未支付取消单。
- `conversion_rate`：`paid_orders / uv`，商品详情页访客支付转化率。
- `return_rate`：`refund_orders / paid_orders`，周期内试穿退货退款单量占比（精品女装日常警戒基线 18%，超过 25% 触发版型/面料品质归因预警）。
- `gross_margin_rate`：`(gmv - 成衣出厂成本 - 整烫包装仓配费) / gmv`，毛利率。
"""


# ============================================================================
# 5. Market & ERP 域：女装行业趋势、价格带空白与成衣供应链成本测算基准
# ============================================================================

MARKET_TRENDS_DB: dict[str, dict[str, Any]] = {
    "连衣裙": {
        "category": "连衣裙",
        "search_growth_yoy": 0.62,
        "benchmark_return_rate": 0.180,
        "hot_keywords": [
            "莱赛尔三醋酸防透全衬连衣裙",
            "梨形友好高腰立体A字茶歇裙",
            "抗静电亲肤垂感通勤连衣裙",
        ],
        "underserved_price_bands": [
            {
                "min_price": 329.0,
                "max_price": 499.0,
                "demand_index": 2.48,
                "gap_description": "329-499元价格带兼具双层防透内衬、天然莱赛尔三醋酸抗静电面料与梨形立体放量剪裁的轻奢通勤连衣裙严重缺款",
            }
        ],
        "consumer_pain_points": [
            "浅色连衣裙单层半衬阳光下透肉走光",
            "标准码腰臀比偏窄梨形身材拉链卡胯紧绷",
            "聚酯雪纺干燥季节起静电贴腿显廉价",
        ],
    },
    "衬衫": {
        "category": "衬衫",
        "search_growth_yoy": 0.41,
        "benchmark_return_rate": 0.120,
        "hot_keywords": [
            "重磅三醋酸免烫飘带衬衫",
            "防走光门襟暗扣法式通勤衬衫",
            "天丝亚麻清凉防晒长袖衬衫",
        ],
        "underserved_price_bands": [
            {
                "min_price": 259.0,
                "max_price": 359.0,
                "demand_index": 2.12,
                "gap_description": "259-359元区间兼具垂顺免烫属性与加密防走光暗扣的醋酸混纺通勤衬衫供给稀缺",
            }
        ],
        "consumer_pain_points": [
            "纯棉衬衫洗后褶皱多每日需熨烫",
            "胸前扣距过大抬手易崩开走光",
            "浅色薄款透内衣轮廓",
        ],
    },
    "裤装": {
        "category": "裤装",
        "search_growth_yoy": 0.46,
        "benchmark_return_rate": 0.135,
        "hot_keywords": [
            "双裤长高腰窄版西装阔腿裤",
            "莱赛尔天丝云感微喇牛仔裤",
            "遮假胯宽立体双褶垂坠拖地裤",
        ],
        "underserved_price_bands": [
            {
                "min_price": 269.0,
                "max_price": 389.0,
                "demand_index": 1.95,
                "gap_description": "269-389元区分小个子/高个子双裤长且久坐膝盖不鼓包的高密垂感西装裤存在空白",
            }
        ],
        "consumer_pain_points": [
            "单一裤长小个子拖地需改裤脚、高个子变九分裤",
            "腰臀剪裁扁平显得假胯宽与小腹突出",
            "普通化纤西装裤久坐起皱起球",
        ],
    },
}

SHIPPING_MODE_RATES: dict[str, dict[str, float | int]] = {
    "domestic_truck": {"per_kg_fee": 3.5, "customs_clearance_flat": 0.0, "lead_days": 4, "moq": 50},
    "sea_freight": {"per_kg_fee": 8.0, "customs_clearance_flat": 1200.0, "lead_days": 22, "moq": 200},
    "rail_freight": {"per_kg_fee": 15.5, "customs_clearance_flat": 900.0, "lead_days": 14, "moq": 100},
    "air_freight": {"per_kg_fee": 36.0, "customs_clearance_flat": 800.0, "lead_days": 5, "moq": 50},
}

RETURN_EXCHANGE_POLICY_MD = """# 女装电商退换货履约与成衣供应链选品风控政策（Market & ERP）

## 一、女装消费者退换货履约标准
1. **七天无理由试穿退换**：签收次日起 7 日内成衣完好（品牌吊牌、防调包铅封扣与水洗标齐全，无穿着香水味、粉底口红渍、勾丝或下水洗涤痕迹）支持无理由退换尺码或原路退款。
2. **成衣品质问题包退换**：如里布车缝脱线、浅色裙装宣传防透但实测无内衬严重透光、拉链卡死或面料色牢度不达标，往返顺丰运费与整烫质检损耗由商家承担。

## 二、女装选品立项供应链准入红线
- 候选女装新款扣除综合到岸总成本（成衣FOB出厂价 + 头程干线物流 + 进口面料清关关税 + 蒸汽整烫防尘挂装包材 + 平台技术服务费 5%）后，**净毛利率低于 15%** 的高风险款禁止盲目排产备货（须预留女装试穿退货整烫损耗缓冲）；
- 进口面料海运集拼起订量（MOQ）不得低于 200 件，国内杭嘉湖快反干线/空运首单试销起订量不得低于 50 件。
"""


def clone_sku_catalog() -> dict[str, dict[str, Any]]:
    """返回隔离深拷贝的女装商品库存字典，防止测试进程间状态污染。"""
    return copy.deepcopy(INITIAL_SKU_CATALOG)


def clone_orders_store() -> dict[str, dict[str, Any]]:
    """返回隔离深拷贝的女装订单字典。"""
    return copy.deepcopy(INITIAL_ORDERS_STORE)


def clone_logistics_packages() -> dict[str, dict[str, Any]]:
    """返回隔离深拷贝的物流运单字典。"""
    return copy.deepcopy(INITIAL_LOGISTICS_PACKAGES)
