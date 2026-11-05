<template>
  <ProviderRow :name="backend.name" :identifier="backend.backend_type" :status="status" :tone="tone" :subtitle="t('settings.providers.fromPackage', { name: backend.package })" />
</template>
<script setup>
import { computed } from "vue"
import { useI18n } from "@/utils/i18n"
import ProviderRow from "./ProviderRow.vue"
const props = defineProps({ backend: { type: Object, required: true } })
const { t } = useI18n()
const ownLogin = computed(() => props.backend.credentials && props.backend.credentials !== "api_key")
const status = computed(() => (ownLogin.value ? t("settings.providers.packageManaged") : t("settings.providers.packageKey")))
const tone = computed(() => (ownLogin.value || props.backend.available ? "success" : "info"))
</script>
