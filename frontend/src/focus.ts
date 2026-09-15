// 轻量自定义指令：元素挂载后自动聚焦（用于重命名输入框）。
import type { Directive } from 'vue'

export const vFocus: Directive = {
  mounted(el: HTMLElement) {
    el.focus()
    // 选中已有文本，便于直接覆盖输入
    if (el instanceof HTMLInputElement) el.select()
  },
}
