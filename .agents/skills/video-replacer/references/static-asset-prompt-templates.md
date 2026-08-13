# Static Asset Prompt Templates

Read this file only for a static-asset generation request. Produce exactly one filled prompt per output the user named, then freeze it for the separately authorized ChatGPT Web Image 2 browser branch. Replacement-prompt writing and video API execution remain with their workflow stages; image-quality review is outside this process.

## Contents

- [Layout and subject are separate choices](#layout-and-subject-are-separate-choices)
- [The one hard dependency](#the-one-hard-dependency)
- [Reference-image precedence](#reference-image-precedence)
- [全景图](#全景图)
- [四视图](#四视图)
- [九宫格--四宫格](#九宫格--四宫格)
- [Provenance](#provenance)

## Layout and subject are separate choices

Layout governs the structure of the image. Subject governs its content. Choose the layout from what the user asked for, not from what the subject happens to be.

| Layout | Structure | Usual subjects |
| --- | --- | --- |
| 全景图 | One 16:9 wide establishing view of the whole subject | 场景 / 外景 |
| 四视图 | One 16:9 asset page: a detailed main view plus three secondary views | 人、车外观、其他物品 |
| 九宫格 / 四宫格 | One grid of the same subject across different angles and shot sizes | 车内饰、场景 |

The user names the subject; you derive the layout from this table without asking. 四视图 is a reusable asset-page layout, not a person-specific or vehicle-specific one — use it for any object that needs consistent multi-angle appearance. When the user does name a layout, that choice wins over the default, including a 九宫格/四宫格 for a location.

## The one hard dependency

A grid layout is built on an existing appearance base and always binds one as its reference input:

- A 车内饰 grid binds an exterior image of that same vehicle.
- A 场景 grid binds a 全景图 of that same location.

Any adequate exterior or establishing image of the same subject can serve as the bound base. If the base image is missing, report the missing binding and keep the grid asset blocked until a matching image is supplied.

Every other layout may run without a base image, using its `Without reference image` variant.

## Reference-image precedence

When the user supplies a reference image, use the matching concise `With reference image` template and let that image carry visible appearance. Limit the text to the reference binding, output layout, user-requested changes, and minimum consistency or neutral-presentation anchors. Add one extra visible anchor only when it controls a requested change or prevents a likely identity/layout error.

Use a full `Without reference image` template only when no reference image is supplied. Fill its brackets from the user's written facts and, for vehicle products, the factual observations permitted below. When the user says “this vehicle” or an equivalent phrase, inspect that vehicle image only to extract the make/model/visible trim and body color. Use the most specific confident model designation and omit the model year when uncertain. Ask only when the image is inaccessible or a required model/body-color fact cannot be determined. The user's written correction overrides an image observation. Remove square brackets from a filled production prompt. Preserve the brackets when the user asks to inspect the reusable template itself.

## 全景图

Use for a location or scene establishing reference. Output the matching prompt alone and stop; a grid is a separate request.

### With reference image

```text
参考图，生成同一[地点名称]的16:9外景参考图，保持主要空间关系和整体视觉一致，完整呈现地点，无人物和剧情动作。
```

### Without reference image

```text
生成[地点名称]的外景参考图，[视觉媒介与核心风格]。16:9地点参考图，使用单一宽幅建立视角，完整呈现地点尺度和主要空间关系，关键区域无遮挡、不裁切；默认空场景，只有尺度难以判断时加入一个匿名比例人物，无具名角色和剧情动作。[地点类型、用途、整体尺度和主要空间关系；最能建立地点身份的建筑、道路、植被、陈设、材质与准确标识]。[时间、天气、光线和空气状态；与地点相符的地域、时代、色彩、材质与氛围]。避免[与当前地点直接相关的泛化、重设计、错误地标、现代元素、关键区域遮挡或其他真实生成风险]。

平台设置：画幅比例 16:9（在生成平台侧设置；提示词文字不能改变画布比例）
```

## 四视图

One 16:9 asset page. Replace the bracketed slots and add only visible requirements explicitly supplied by the user; the result remains a single asset-page instruction.

### 人 — with reference image

```text
参考图，生成同一人物的16:9角色资产页：左侧精细半身肖像，右侧A-pose全身正面、侧面、背面三视图。保持人物身份、体型、发型、服装和配饰一致，人物完整入画，中性灰摄影棚背景。
```

### 人 — without reference image

```text
生成[角色名称]的基础角色参考资产，[视觉媒介与核心风格]。16:9角色资产页。左侧为精细半身肖像；右侧为同一角色的A-pose全身正面、侧面、背面三视图，人物从头到脚完整入画。[年龄区间、身份、身高、体型、身体比例、面部与骨骼特征、肤色和稳定气质]。[服装款式、层次、廓形、穿着方式、衣料和维护状态]；[发长、发型、分缝、蓬松或贴合状态、发饰及固定方式]；[底妆质感、眉眼、唇色、修饰程度，以及年龄、职业和生活状态留下的可见特征]；[饰品、眼镜、帽子、鞋子和随身佩戴物]；[服装、头发、妆面和配饰的主色、辅助色与明暗关系]；[基准状态下的整洁度、精神状态、身体维护程度和生活痕迹]。[与角色有关的地域、时代、气候、职业、阶层和生活条件形成的可见细节]。[角色造型、材质、色彩和媒介质感的视觉表现]。均匀柔和的摄影棚参考光，身体结构与完整妆造清晰可读，无戏剧性光效。纯净的中性灰色摄影棚背景，无场景、道具、文字或装饰。避免[身份混杂、年龄或体型漂移、三视图妆造不一致、脚部出框、塑料感皮肤、时尚大片化或其他真实生成风险]。

平台设置：画幅比例 16:9（在生成平台侧设置；提示词文字不能改变画布比例）
```

### 车外观 — with reference image

```text
参考图，生成同一辆[车型] [车身颜色]车辆的16:9外观四视图资产页：前侧45°、正面、标准侧面、正后方。保持车型、颜色和外观配置一致，车辆完整入画，中性灰摄影棚背景。
```

### 车外观 — without reference image

```text
生成[车型] [车身颜色]车身的基础车辆外观参考资产，写实专业汽车产品摄影。16:9车辆资产页。左侧为精细前侧45°主视图；右侧为同一车辆的完整正面、标准侧面、正后方三视图。车辆完整入画，车型比例、车身线条、灯组、格栅、轮毂及外观配置在全部视图中严格一致。均匀柔和的摄影棚产品光，车身结构与漆面清晰可读。纯净中性灰色摄影棚背景，无人物、道具、文字或装饰。避免车型混杂、视图间配置漂移、车身裁切和颜色偏差。
```

### 其他物品 — with reference image

```text
参考图，生成同一[物品名称]的16:9物品资产页：左侧精细主视图，右侧同一物品的正面、标准侧面、背面三视图。保持形态、结构、材质、颜色和表面细节一致，物品完整入画，中性灰摄影棚背景。
```

### 其他物品 — without reference image

```text
生成[物品名称]的基础物品参考资产，[视觉媒介与核心风格]。16:9物品资产页。左侧为精细主视图；右侧为同一物品的正面、标准侧面、背面三视图，物品完整入画不裁切。[整体尺度、比例、形态与结构关系]；[主要部件、开合或活动方式、连接与固定方式]；[材质、表面处理、纹理与磨损状态]；[主色、辅助色与明暗关系]；[品牌标识或文字，仅在用户提供准确内容时写入]。均匀柔和的摄影棚产品光，结构与材质清晰可读，无戏剧性光效。纯净中性灰色摄影棚背景，无人物、场景、道具或装饰。避免[形态漂移、视图间结构或颜色不一致、部件缺失、裁切、杜撰标识或其他真实生成风险]。

平台设置：画幅比例 16:9（在生成平台侧设置；提示词文字不能改变画布比例）
```

## 九宫格 / 四宫格

Bind the appearance base image described above and keep each prompt to one sentence at grid level. The prompt names the grid size and consistency target while leaving individual views and components to the generator. Use 四宫格 in place of 九宫格 only when the user asks for it, changing that one word.

### 车内饰

```text
以车辆外观参考图作为参考图，生成同一[车型]内饰不同角度不同景别的九宫格场景图，保持内饰一致，灰色摄影棚背景，专业摄影光影。
```

### 场景

```text
以外景全景参考图作为参考图，生成同一[地点名称]不同角度不同景别的九宫格场景图，保持地点空间关系和整体视觉一致，无人物和剧情动作。
```

## Provenance

These templates are maintained as part of Video Replacer. They combine reusable multi-view asset-page patterns with user-approved vehicle-interior and location-grid variants. Keep future provenance references public and resolvable; do not add private skill names or local account identifiers.
