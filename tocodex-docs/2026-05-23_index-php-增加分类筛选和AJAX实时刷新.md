# 2026-05-23 index.php 增加分类筛选和 AJAX 实时刷新

## 修改文件
- [`php-predict/index.php`](php-predict/index.php)

## 变更内容

### 1. Tab 栏（CSS + HTML）
- 在 `<style>` 块中新增 `.tab-bar`、`.tab-btn`、`.tab-btn.active` 样式
- 在「今日预测」section-title 下方、refresh-bar 上方插入 Tab 切换栏
- 三个 Tab：「全部」|「正在比赛」|「已结束」，默认选中「全部」

### 2. 增强刷新栏
- 替换原有简单刷新按钮为：刷新时间显示（`#refreshInfo`）+ 手动刷新按钮（`#manualRefreshBtn`）

### 3. 数据属性
- 预测表格 div 添加 `id="todayPredictions"`
- 每行 `<tr>` 添加 `data-match-status` 和 `data-pred-status` 属性
- 比分 `<td>` 添加 `data-match-id` 属性用于 AJAX 定位更新

### 4. JavaScript（页面底部 `<script>` 标签）
- **Tab 切换**：根据 `data-match-status` 过滤行显示/隐藏
  - 正在比赛：匹配 LIVE / HT / 进行中
  - 已结束：匹配 FT / 完 / FINISHED 或 pred_status=settled
- **AJAX 自动刷新**：每 30s 调用 `/matches/status?filter=live`
  - 支持 ETag/If-None-Match 减少不必要传输
  - 比分变化时添加 `score-pulse` 脉冲动画
  - 首轮延迟 5s 触发，避免页面加载时立即请求
- **手动刷新**：点击按钮立即触发一次轮询

### 5. 脉冲动画 CSS
- `@keyframes scorePulse`：缩放 + 颜色变化（绿→金→绿）
- `.score-pulse` class 触发 0.6s 动画