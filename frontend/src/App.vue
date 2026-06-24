<template>
  <v-app>
    <v-app-bar title="TraceVector" color="primary" density="compact">
      <template #prepend>
        <v-app-bar-nav-icon @click="drawer = !drawer" />
      </template>
    </v-app-bar>
    <v-navigation-drawer v-model="drawer" temporary>
      <v-list>
        <v-list-item to="/" title="Home" prepend-icon="mdi-home" />
        <v-list-item to="/cases" title="Cases" prepend-icon="mdi-briefcase" />
      </v-list>
    </v-navigation-drawer>
    <v-main>
      <router-view />
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import { ref } from "vue";

const drawer = ref(false);
</script>
