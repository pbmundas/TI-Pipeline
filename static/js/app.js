const { createApp } = Vue;

const themeService = {
    init() {
        const saved = localStorage.getItem('theme');
        if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
            document.documentElement.setAttribute('data-theme', 'dark');
        }
    },
    toggle() {
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
        localStorage.setItem('theme', isDark ? 'light' : 'dark');
    },
    isDark() {
        return document.documentElement.getAttribute('data-theme') === 'dark';
    }
};

themeService.init();

var app = createApp({
    data: function () {
        return {
            loading: true,
            error: null,
            report: {
                metadata: {},
                sources: [],
                executive_brief: '',
                threat_stories: [],
                actor_profiles: [],
                critical_vulnerabilities: [],
                hunting_leads: [],
                statistics: {}
            },
            latestReport: null,
            archive: [],
            archiveLoading: false,
            archiveError: null,
            viewingArchivedDate: null,
            activeTab: 'sources',
            tabs: [
                { id: 'brief', label: 'Brief', icon: 'bi bi-file-earmark-text' },
                { id: 'stories', label: 'Stories', icon: 'bi bi-lightning' },
                { id: 'actors', label: 'Actors', icon: 'bi bi-person-fill' },
                { id: 'vulns', label: 'Vulnerabilities', icon: 'bi bi-shield-exclamation' },
                { id: 'hunting', label: 'Hunting', icon: 'bi bi-crosshair' },
                { id: 'sources', label: 'Sources', icon: 'bi bi-link-45deg' },
                { id: 'statistics', label: 'Statistics', icon: 'bi bi-bar-chart' },
                { id: 'archive', label: 'Archive', icon: 'bi bi-archive' }
            ],
            expandedStories: {},
            sourceSearch: ''
        };
    },

    computed: {
        filteredSources: function () {
            if (!this.sourceSearch) return this.report.sources;
            var q = this.sourceSearch.toLowerCase();
            return this.report.sources.filter(function (s) {
                return s.title.toLowerCase().indexOf(q) !== -1 ||
                    (s.source_type && s.source_type.toLowerCase().indexOf(q) !== -1);
            });
        }
    },

    mounted: function () {
        this.loadReport();
        this.loadArchive();
    },

    methods: {
        loadReport: function () {
            var self = this;
            self.loading = true;
            self.error = null;
            var permalinkMatch = window.location.pathname.match(/\/reports\/(\d{4}-\d{2}-\d{2})\.html$/);
            var reportUrl = permalinkMatch
                ? './reports/' + permalinkMatch[1] + '.json'
                : './gui/unified_report.json';
            if (permalinkMatch) self.viewingArchivedDate = permalinkMatch[1];
            // Prefer the fully Ollama-enriched report if it's been published.
            // Falls back to the raw article cache (sources only, no AI
            // sections) so the dashboard still works before the first
            // enrichment run.
            fetch(reportUrl)
                .then(function (res) {
                    if (!res.ok) throw new Error('no enriched report');
                    return res.json();
                })
                .then(function (data) {
                    self.report = data;
                    if (!permalinkMatch) self.latestReport = data;
                })
                .catch(function () {
                    return self.loadFromRawArticles();
                })
                .catch(function (e) {
                    console.error('Failed to load articles:', e);
                    self.error = 'Failed to load articles. Please try again.';
                })
                .finally(function () {
                    self.loading = false;
                });
        },

        loadFromRawArticles: function () {
            var self = this;
            return fetch('./data/articles.json')
                .then(function (res) {
                    if (!res.ok) throw new Error('Failed to load articles: ' + res.status);
                    return res.json();
                })
                .then(function (data) {
                    // data/articles.json is a flat array of raw collected
                    // articles (no LLM enrichment). Adapt it into the same
                    // report shape the rest of the UI expects, assigning a
                    // stable sequential id to each article for source links.
                    var sources = data.map(function (a, idx) {
                        return {
                            id: idx + 1,
                            title: a.title,
                            url: a.url,
                            published_date: a.published_date,
                            source_type: a.source_type || ''
                        };
                    });
                    sources.sort(function (a, b) {
                        return new Date(b.published_date) - new Date(a.published_date);
                    });

                    self.report = {
                        metadata: {
                            generated_at: new Date().toISOString(),
                            report_version: '1.0',
                            format: 'raw_articles',
                            documents_analyzed: sources.length,
                            time_period_days: null,
                            generated_by: 'data/articles.json (no LLM enrichment yet)'
                        },
                        sources: sources,
                        executive_brief: '',
                        threat_stories: [],
                        actor_profiles: [],
                        critical_vulnerabilities: [],
                        hunting_leads: [],
                        statistics: {
                            top_actors: [],
                            top_targeted_industries: [],
                            emerging_trends: [],
                            declining_threats: [],
                            key_changes: ''
                        }
                    };
                    self.latestReport = self.report;
                });
        },

        loadArchive: function () {
            var self = this;
            self.archiveLoading = true;
            self.archiveError = null;
            return fetch('./reports/index.json', { cache: 'no-store' })
                .then(function (res) {
                    if (res.status === 404) return [];
                    if (!res.ok) throw new Error('Archive index request failed: ' + res.status);
                    return res.json();
                })
                .then(function (items) {
                    self.archive = Array.isArray(items) ? items : [];
                })
                .catch(function (e) {
                    console.error('Failed to load report archive:', e);
                    self.archiveError = 'The report archive is temporarily unavailable.';
                })
                .finally(function () { self.archiveLoading = false; });
        },

        reportHtmlUrl: function (entry) {
            // Never trust legacy archive indexes for the public link: older
            // entries used a JSON URL. Build an absolute HTML permalink from
            // the stable report date so copy-link/open-in-new-tab is correct.
            return new URL('reports/' + entry.date + '.html', document.baseURI).href;
        },

        returnToLatest: function () {
            if (this.latestReport) {
                this.report = this.latestReport;
                this.viewingArchivedDate = null;
                this.activeTab = 'archive';
                window.history.pushState({}, '', './');
            } else {
                window.location.href = './';
            }
        },

        formatDate: function (dateStr) {
            if (!dateStr) return '';
            try {
                var d = new Date(dateStr);
                return d.toLocaleDateString('en-US', {
                    year: 'numeric',
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit'
                });
            } catch (e) {
                return dateStr;
            }
        },

        toggleStory: function (idx) {
            this.expandedStories[idx] = !this.expandedStories[idx];
            this.expandedStories = Object.assign({}, this.expandedStories);
        },

        getSourceUrl: function (id) {
            var src = this.report.sources.find(function (s) { return s.id === id; });
            return src ? src.url : '#';
        },

        formatMarkdown: function (text) {
            if (!text) return '';
            return marked.parse(text, { breaks: true });
        },

        impactClass: function (impact) {
            if (!impact) return '';
            var s = impact.toLowerCase();
            if (s.indexOf('critical') !== -1) return 'severity-critical';
            if (s.indexOf('high') !== -1) return 'severity-high';
            if (s.indexOf('medium') !== -1) return 'severity-medium';
            return '';
        },

        severityClass: function (severity) {
            if (!severity) return '';
            var s = severity.toLowerCase();
            if (s === 'critical') return 'severity-critical';
            if (s === 'high') return 'severity-high';
            if (s === 'medium') return 'severity-medium';
            return '';
        },

        cvssClass: function (score) {
            if (score >= 9) return 'cvss-critical';
            if (score >= 7) return 'cvss-high';
            if (score >= 4) return 'cvss-medium';
            return 'cvss-low';
        },

        motivationClass: function (motivation) {
            if (!motivation) return '';
            var s = motivation.toLowerCase();
            if (s.indexOf('espionage') !== -1) return 'motivation-Espionage';
            if (s.indexOf('financial') !== -1) return 'motivation-Financial';
            if (s.indexOf('disruption') !== -1 || s.indexOf('political') !== -1) return 'motivation-Disruption';
            return '';
        },

        exploitClass: function (status) {
            if (!status) return '';
            var s = status.toLowerCase();
            if (s.indexOf('active') !== -1) return 'exploit-active';
            if (s.indexOf('confirmed') !== -1 || s.indexOf('functional') !== -1) return 'exploit-functional';
            return 'exploit-unknown';
        }
    }
});

app.config.compilerOptions.delimiters = ['[[', ']]'];
app.mount('#app');

var themeToggleBtn = document.getElementById('themeToggleBtn');
if (themeToggleBtn) {
    var updateIcon = function () {
        var icon = themeToggleBtn.querySelector('i');
        if (icon) {
            icon.className = themeService.isDark() ? 'bi bi-sun' : 'bi bi-moon-stars';
        }
        themeToggleBtn.setAttribute('aria-label', themeService.isDark() ? 'Switch to light mode' : 'Switch to dark mode');
    };
    themeToggleBtn.addEventListener('click', function () {
        themeService.toggle();
        updateIcon();
    });
    updateIcon();
}
