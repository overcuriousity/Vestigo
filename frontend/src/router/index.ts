import { createRouter, createWebHistory } from "vue-router";
import HomeView from "../views/HomeView.vue";
import CaseListView from "../views/CaseListView.vue";
import CaseDetailView from "../views/CaseDetailView.vue";
import TimelineDetailView from "../views/TimelineDetailView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "home", component: HomeView },
    { path: "/cases", name: "cases", component: CaseListView },
    { path: "/cases/:caseId", name: "case", component: CaseDetailView },
    {
      path: "/cases/:caseId/timelines/:timelineId",
      name: "timeline",
      component: TimelineDetailView,
    },
  ],
});

export default router;
