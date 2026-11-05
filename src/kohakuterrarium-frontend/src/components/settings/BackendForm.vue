<template>
  <div class="grid grid-cols-[1fr_1fr] gap-3">
    <div>
      <label class="text-[11px] text-warm-400 mb-1 block">{{ t("settings.backends.name") }}</label>
      <el-input :model-value="form.name" size="small" placeholder="my-provider" :disabled="isEditing" @update:model-value="updateField('name', $event)" />
    </div>
    <div>
      <label class="text-[11px] text-warm-400 mb-1 block">{{ t("settings.backends.backendType") }}</label>
      <el-select :model-value="form.backend_type" size="small" class="w-full" @update:model-value="updateField('backend_type', $event)">
        <el-option v-for="type in typeOptions" :key="type.name" :value="type.name" :label="type.label" />
      </el-select>
    </div>
    <div v-if="needsEndpoint" class="col-span-2">
      <label class="text-[11px] text-warm-400 mb-1 block">{{ t("settings.backends.baseUrl") }}</label>
      <el-input :model-value="form.base_url" size="small" placeholder="https://api.example.com/v1" @update:model-value="updateField('base_url', $event)" />
    </div>
    <div class="col-span-2">
      <label class="text-[11px] text-warm-400 mb-1 block">{{ t("settings.backends.providerName") }}</label>
      <el-input :model-value="form.provider_name" size="small" :placeholder="form.name || 'my-provider'" @update:model-value="updateField('provider_name', $event)" />
      <p class="text-[10px] text-warm-400 mt-1">
        {{ t("settings.backends.providerNameHint") }}
      </p>
    </div>
    <div v-if="nativeToolCatalog.length" class="col-span-2">
      <label class="text-[11px] text-warm-400 mb-1 block">{{ t("settings.backends.nativeTools") }}</label>
      <el-checkbox-group :model-value="form.provider_native_tools" class="flex flex-col gap-1" @update:model-value="updateField('provider_native_tools', $event)">
        <el-checkbox v-for="tool in nativeToolCatalog" :key="tool.name" :value="tool.name" class="!mr-0">
          <span class="font-mono text-[12px]">{{ tool.name }}</span>
          <span class="text-[10px] text-warm-400 ml-1"> ({{ tool.provider_support.length ? tool.provider_support.join(", ") : "any" }}) </span>
          <span v-if="tool.description" class="text-[10px] text-warm-400 ml-1 truncate">— {{ tool.description }}</span>
        </el-checkbox>
      </el-checkbox-group>
      <p class="text-[10px] text-warm-400 mt-1">
        {{ t("settings.backends.nativeToolsHint") }}
      </p>
    </div>
    <div class="col-span-2 flex justify-end gap-2">
      <el-button size="small" @click="$emit('cancel')">
        {{ t("common.cancel") }}
      </el-button>
      <el-button type="primary" size="small" :disabled="!form.name || !form.backend_type" @click="$emit('save')">
        {{ t("common.save") }}
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue"
import { settingsAPI } from "@/utils/api"
import { useI18n } from "@/utils/i18n"

const props = defineProps({
  form: { type: Object, required: true },
  nativeToolCatalog: { type: Array, default: () => [] },
  isEditing: { type: Boolean, default: false },
})

const emit = defineEmits(["save", "cancel", "update-field"])

const { t } = useI18n()

const DEFAULT_TYPES = [
  { name: "openai", label: "openai", credentials: "api_key" },
  { name: "codex", label: "openai-responses (codex)", credentials: "api_key" },
  { name: "anthropic", label: "anthropic", credentials: "api_key" },
]
const packageTypes = ref([])
const typeOptions = computed(() => [...DEFAULT_TYPES, ...packageTypes.value])
const needsEndpoint = computed(() => !["external", "none"].includes(typeOptions.value.find((type) => type.name === props.form.backend_type)?.credentials))

onMounted(async () => {
  try {
    const data = await settingsAPI.getBackendTypes()
    packageTypes.value = (data.backend_types || []).filter((type) => type.source === "package").map((type) => ({ name: type.name, label: `${type.display_name || type.name} (${type.package})`, credentials: type.credentials }))
  } catch {
    packageTypes.value = []
  }
})

function updateField(key, value) {
  emit("update-field", { key, value })
}
</script>
