// 菜单视觉层配置（A24）。只放两类东西：**运营位** 和 **可核实的卖点**。
//
// 三条硬规则（写论文/答辩被问「凭什么这么写」时按这个答）：
// 1. 主推位由运营指定，不由销量推断——本系统不采集销量，编「热销第一」就是造假；
// 2. 卖点只写能从店里数据核实的：配方净重（dish_recipe_items）、参考出餐时长
//    （dishes.prep_minutes）、做法（SOP）；核实不了的菜宁可不写；
// 3. 「招牌」「秘制」这类无据说法一律不用，改成可查的事实。
module.exports = {
  // 首屏横幅（tools/menu_assets.py 生成，见 docs/menu_assets/manifest.json）
  banner: '/assets/banner/menu_banner.jpg',

  // 主推位：菜码 → 一句话卖点。菜码与 fixtures/seed_kitchen.sql 一致。
  featured: {
    'D-QJRS': '里脊 150g · 青椒 100g',
    'D-GBJD': '鸡胸 150g · 花生米 40g',
    'D-SYR': '现片现煮 · 参考 25 分钟',
    'D-KSJD': '整鸭烤制 · 配薄饼葱丝'
  },

  // 沽清遮罩上的短文案（菜名下面还有完整原因，这里只求一眼看懂）
  soldOutMask: '已沽清'
};
