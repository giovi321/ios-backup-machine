import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://giovi321.github.io',
  base: '/ios-backup-machine',
  integrations: [
    starlight({
      title: 'iOS Backup Machine',
      description: 'Offline, automatic iPhone backup appliance — documentation',
      components: {
        Head: './src/components/Head.astro',
      },
      customCss: ['./src/styles/diagrams.css'],
      head: [
        {
          tag: 'link',
          attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' },
        },
        {
          tag: 'link',
          attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' },
        },
        {
          tag: 'link',
          attrs: {
            rel: 'stylesheet',
            href: 'https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap',
          },
        },
      ],
      logo: {
        src: './src/assets/logo.svg',
        replacesTitle: false,
      },
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/giovi321/ios-backup-machine',
        },
      ],
      editLink: {
        baseUrl: 'https://github.com/giovi321/ios-backup-machine/edit/main/docs/',
      },
      sidebar: [
        { label: 'Home', link: '/' },
        {
          label: 'Getting started',
          items: [
            { label: 'Overview', link: '/getting-started/overview/' },
            { label: 'Hardware', link: '/getting-started/hardware/' },
            { label: 'Installation', link: '/getting-started/installation/' },
            { label: 'First backup', link: '/getting-started/first-backup/' },
          ],
        },
        {
          label: 'Guide',
          items: [
            { label: 'Display and controls', link: '/guide/display-and-controls/' },
            { label: 'Backups', link: '/guide/backups/' },
            { label: 'Remote sync', link: '/guide/remote-sync/' },
            { label: 'Networking', link: '/guide/networking/' },
            { label: 'WireGuard VPN', link: '/guide/wireguard-vpn/' },
            { label: 'Web UI', link: '/guide/web-ui/' },
            { label: 'Logs', link: '/guide/logs/' },
          ],
        },
        {
          label: 'Architecture',
          items: [
            { label: 'Overview', link: '/architecture/overview/' },
            { label: 'Device connectivity', link: '/architecture/device-connectivity/' },
            { label: 'Security', link: '/architecture/security/' },
          ],
        },
        {
          label: 'Development',
          items: [
            { label: 'Contributing', link: '/development/contributing/' },
            { label: 'Testing', link: '/development/testing/' },
          ],
        },
      ],
    }),
  ],
});
