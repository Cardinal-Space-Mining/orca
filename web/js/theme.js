// Light and dark themes. The choice belongs to the screen, not to Orca, so it
// is kept in this browser (localStorage): 'dark', 'light' or 'system'. The
// small script at the top of index.html applies it before the page draws, so
// there is no flash of the other theme.

import { useEffect, useState } from 'preact/hooks';


const STORAGE_KEY = 'orca-theme';
export const THEME_CHOICES = ['system', 'dark', 'light'];
const systemQuery = window.matchMedia('(prefers-color-scheme: light)');
const listeners = new Set();

export function themeChoice() {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return THEME_CHOICES.includes(stored) ? stored : 'system';
}

function themeFor(choice) {
    if (choice === 'system') return systemQuery.matches ? 'light' : 'dark';
    return choice;
}

export function currentTheme() {
    return document.documentElement.dataset.theme || themeFor(themeChoice());
}

function applyTheme() {
    const theme = themeFor(themeChoice());
    document.documentElement.dataset.theme = theme;
    const icon = document.querySelector('link[rel="icon"]');
    if (icon) icon.href = logoFor(theme);
    for (const listener of listeners) listener();
}

export function setThemeChoice(choice) {
    window.localStorage.setItem(STORAGE_KEY, choice);
    applyTheme();
}

// The toggle in the top bar: flip what is shown now. Choosing the theme the
// system would pick anyway goes back to following the system.
export function toggleTheme() {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    setThemeChoice(next === themeFor('system') ? 'system' : next);
}

// The logo drawn for a background: light parts on dark, dark on light.
export function logoFor(theme) {
    return `/static/orca-gear-logo-${theme}.svg`;
}

// Re-renders when the theme changes (toggle, settings, or the system's own
// light/dark switch while following it).
export function useTheme() {
    const [, setVersion] = useState(0);
    useEffect(() => {
        const listener = () => setVersion((version) => version + 1);
        listeners.add(listener);
        return () => listeners.delete(listener);
    }, []);
    return { theme: currentTheme(), choice: themeChoice() };
}

// Terminals are drawn by xterm.js, which needs plain colours.
export function onThemeChange(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
}

systemQuery.addEventListener('change', () => {
    if (themeChoice() === 'system') applyTheme();
});
window.addEventListener('storage', (event) => {
    if (event.key === STORAGE_KEY) applyTheme();  // another tab changed it
});
applyTheme();
