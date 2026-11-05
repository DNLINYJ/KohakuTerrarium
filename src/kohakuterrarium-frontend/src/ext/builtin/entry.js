import BudgetPanel from "@/ext/builtin/BudgetPanel.vue"
import { defineExtension } from "@/ext/sdk/index"

export default defineExtension((kt) => {
  kt.registerPanel("budget", BudgetPanel)
})
