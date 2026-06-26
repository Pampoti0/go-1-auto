module.exports = {
  darkMode: "class",
  content: ["./frontend/index.template.html", "./frontend/app.jsx"],
  theme: {
    extend: {
      colors: {
        accent: "#0ea371",
        accent2: "#0891b2",
      },
    },
  },
  safelist: ["hidden", "block", "animate-spin", "animate-pulse"],
};
