// 点菜页：菜单浏览（搜索/分类/沽清筛选）→ 加购 → 去确认页。
// 购物车存在 utils/store.js（本地落盘），跨页面、跨冷启动保留。
var api = require('../../utils/api.js');
var store = require('../../utils/store.js');
var fmt = require('../../utils/format.js');

Page({
  data: {
    dishes: [],          // 全量（带 qty 字段，供 WXML 渲染加减）
    visible: [],         // 过滤后的展示列表
    categories: ['全部'],
    activeCategory: '全部',
    keyword: '',
    hideSoldOut: false,
    cartQty: 0,
    cartMoney: '0.00',
    loading: true,
    error: '',
    detail: null         // 菜品详情弹层
  },

  onLoad: function () { this.reload(); },

  onShow: function () {
    // 从确认页/别的 tab 回来时，购物车可能已经变了
    if (this.data.dishes.length) { this.syncCart(); }
  },

  onPullDownRefresh: function () {
    var that = this;
    this.reload().then(function () { wx.stopPullDownRefresh(); });
  },

  retry: function () { this.reload(); },

  emptyAction: function () {
    this.setData({ keyword: '', activeCategory: '全部', hideSoldOut: false });
    this.applyFilter();
  },

  reload: function () {
    var that = this;
    this.setData({ loading: true, error: '' });
    return api.request('GET', '/api/customer/menu').then(function (data) {
      var dishes = (data.dishes || []).map(function (d) {
        return {
          id: d.id, code: d.code, name: d.name,
          category: d.category || '其他',
          price: d.price, price_text: fmt.money(d.price),
          sold_out: !!d.sold_out, estimate_minutes: d.estimate_minutes || 0,
          qty: 0
        };
      });
      // 菜单刷新后把已沽清项剔出购物车：否则要到提交时才被 409 整单拒绝
      store.pruneSoldOut(dishes);
      var cats = ['全部'];
      dishes.forEach(function (d) {
        if (cats.indexOf(d.category) < 0) { cats.push(d.category); }
      });
      that.setData({ dishes: dishes, categories: cats, loading: false, error: '' });
      that.syncCart(dishes);
    }).catch(function (err) {
      that.setData({ loading: false, error: err.message || '菜单加载失败' });
    });
  },

  // 购物车 → 菜单卡片上的数量 + 底部栏合计（一次 setData 完成）
  syncCart: function (dishes) {
    var rows = store.getCart();
    var summary = store.cartSummary(rows);
    var qtyMap = {};
    rows.forEach(function (row) { qtyMap[row.code] = row.qty; });
    var list = (dishes || this.data.dishes).map(function (d) {
      var copy = {};
      Object.keys(d).forEach(function (k) { copy[k] = d[k]; });
      copy.qty = qtyMap[d.code] || 0;
      return copy;
    });
    this.setData({
      dishes: list, cartQty: summary.qty, cartMoney: summary.money_text
    });
    this.applyFilter(list);
  },

  applyFilter: function (dishes) {
    var that = this;
    var kw = (this.data.keyword || '').trim().toLowerCase();
    var cat = this.data.activeCategory;
    var hide = this.data.hideSoldOut;
    var rows = (dishes || this.data.dishes).filter(function (d) {
      if (cat !== '全部' && d.category !== cat) { return false; }
      if (hide && d.sold_out) { return false; }
      if (kw && (d.name + ' ' + d.category + ' ' + d.code).toLowerCase().indexOf(kw) < 0) {
        return false;
      }
      return true;
    });
    this.setData({ visible: rows });
  },

  onSearch: function (e) {
    this.setData({ keyword: e.detail.value });
    this.applyFilter();
  },

  clearSearch: function () {
    this.setData({ keyword: '' });
    this.applyFilter();
  },

  selectCategory: function (e) {
    this.setData({ activeCategory: e.currentTarget.dataset.cat });
    this.applyFilter();
  },

  toggleSoldOut: function () {
    this.setData({ hideSoldOut: !this.data.hideSoldOut });
    this.applyFilter();
  },

  findDish: function (code) {
    var list = this.data.dishes;
    for (var i = 0; i < list.length; i++) {
      if (list[i].code === code) { return list[i]; }
    }
    return null;
  },

  add: function (e) {
    var dish = this.findDish(e.currentTarget.dataset.code);
    if (!dish || dish.sold_out) { return; }
    store.addToCart(dish);
    this.syncCart();
  },

  minus: function (e) {
    store.changeQty(e.currentTarget.dataset.code, -1);
    this.syncCart();
  },

  openDetail: function (e) {
    var dish = this.findDish(e.currentTarget.dataset.code);
    if (dish) { this.setData({ detail: dish }); }
  },

  closeDetail: function () { this.setData({ detail: null }); },

  noop: function () { /* 弹层内部点击不冒泡关闭 */ },

  addFromDetail: function () {
    if (this.data.detail && !this.data.detail.sold_out) {
      store.addToCart(this.data.detail);
      this.syncCart();
      this.setData({ detail: null });
    }
  },

  goCart: function () {
    wx.navigateTo({ url: '/pages/cart/cart' });
  }
});
