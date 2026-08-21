/** Generated from trigger.cron; do not hand-edit. */
import { defineSchedule } from "eve/schedules";

export default defineSchedule({
  cron: "17 8 * * *",
  markdown: "Once a day, fetch the Hacker News front page, pick the posts about AI, and write a three-line summary into the inbox. Never post, comment, or vote.",
});
