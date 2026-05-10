/** Tailwind config for Newspaparr.
 *  Built via the standalone CLI (no npm). See scripts/build-css.sh.
 */
module.exports = {
  darkMode: 'class',
  content: [
    './templates/**/*.html',
    './icons.py',     // class strings passed to icon() helper
    './app.py',       // class strings in flash messages, render contexts
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50:  '#eef2ff',
          100: '#e0e7ff',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          900: '#312e81',
        },
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
