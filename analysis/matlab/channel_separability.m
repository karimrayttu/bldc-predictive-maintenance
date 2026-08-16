function channel_separability(runsCsv, outDir)
%CHANNEL_SEPARABILITY  Score every telemetry channel as a single-fault detector.
%
%   For each fault condition, each channel is treated as a one-feature
%   detector against the healthy runs and scored by the area under its ROC
%   curve. AUC 1.0 means the channel separates that fault perfectly on its
%   own; 0.5 means it carries no information about it. A rank-sum p value and
%   Cliff's delta are reported alongside, because with as few as six runs in a
%   class an AUC on its own is easy to over-read.
%
%   The AUC reported here is DIRECTION-FREE: max(a, 1-a). That is deliberate
%   (a channel that drops on a fault detects it just as well as one that
%   rises) but it means the statistic cannot fall below 0.5 by construction.
%   An AUC near 0.5-0.6 is therefore the floor, not a measurement, and only
%   the p value tells you whether it is distinguishable from chance.
%
%   Input is figures/runs.csv, which tools/plot_dataset.py writes over the
%   runs tagged valid_for_classifier="true"; the same set the classifier is
%   trained and tested on.
%
%   This answers the question the classifier cannot: which sensor is actually
%   detecting which fault, and which channels are dead weight.
%
%   Usage:
%       channel_separability                       % uses figures/runs.csv
%       channel_separability('figures/runs.csv', 'figures')
%
%   Run headless:
%       matlab -batch "addpath('analysis/matlab'); channel_separability"

    here = fileparts(mfilename('fullpath'));
    repo = fileparts(fileparts(here));

    if nargin < 1 || isempty(runsCsv)
        runsCsv = fullfile(repo, 'figures', 'runs.csv');
    end
    if nargin < 2 || isempty(outDir)
        outDir = fullfile(repo, 'figures');
    end
    if ~isfile(runsCsv)
        error('channel_separability:missingInput', ...
              '%s not found. Run: python tools/plot_dataset.py', runsCsv);
    end

    T = readtable(runsCsv, 'TextType', 'string');
    channels = ["accel_rms_mg", "accel_pk_mg", "i_dc_a", "ripple_permil", ...
                "temp_motor_c", "rpm"];
    labels   = ["vibration RMS", "vibration peak", "bus current", ...
                "speed ripple", "temperature", "speed"];

    healthy = T(T.condition == "healthy", :);
    faults  = setdiff(unique(T.condition), "healthy", 'stable');

    auc   = nan(numel(channels), numel(faults));
    pval  = nan(numel(channels), numel(faults));
    delta = nan(numel(channels), numel(faults));

    fprintf('\n%-16s', 'channel');
    fprintf('%22s', faults{:});
    fprintf('\n%s\n', repmat('-', 1, 16 + 22*numel(faults)));

    for c = 1:numel(channels)
        fprintf('%-16s', labels(c));
        for f = 1:numel(faults)
            ref  = healthy.(char(channels(c)));
            test = T.(char(channels(c)))(T.condition == faults(f));
            ref  = ref(~isnan(ref));
            test = test(~isnan(test));
            if numel(test) < 2 || numel(ref) < 2
                fprintf('%22s', 'n/a');
                continue
            end

            scores = [ref; test];
            group  = [zeros(numel(ref),1); ones(numel(test),1)];

            % Direction-free AUC: a channel that drops on a fault is just as
            % useful as one that rises, so take whichever direction is stronger.
            a = auc_from(scores, group);
            auc(c,f) = max(a, 1 - a);

            pval(c,f)  = ranksum(ref, test);
            delta(c,f) = cliffs_delta(ref, test);

            fprintf('%14.3f p%-7.3g', auc(c,f), pval(c,f));
        end
        fprintf('\n');
    end

    writetable(results_table(channels, labels, faults, auc, pval, delta), ...
               fullfile(outDir, 'channel_separability.csv'));

    plot_auc(auc, labels, faults, outDir);

    fprintf('\nwrote %s\n', fullfile(outDir, 'channel_separability.csv'));
    fprintf('wrote %s\n', fullfile(outDir, 'channel_separability.png'));
end


function a = auc_from(scores, group)
    [~, ~, ~, a] = perfcurve(group, scores, 1);
end


function d = cliffs_delta(a, b)
%CLIFFS_DELTA  Non-parametric effect size, robust at these sample sizes.
    more = 0; less = 0;
    for i = 1:numel(a)
        more = more + sum(b > a(i));
        less = less + sum(b < a(i));
    end
    d = (more - less) / (numel(a) * numel(b));
end


function T = results_table(channels, labels, faults, auc, pval, delta)
    rows = numel(channels) * numel(faults);
    channel = strings(rows,1); label = strings(rows,1);
    condition = strings(rows,1);
    AUC = nan(rows,1); p = nan(rows,1); cliffs = nan(rows,1);
    k = 0;
    for c = 1:numel(channels)
        for f = 1:numel(faults)
            k = k + 1;
            channel(k)   = channels(c);
            label(k)     = labels(c);
            condition(k) = faults(f);
            AUC(k)       = auc(c,f);
            p(k)         = pval(c,f);
            cliffs(k)    = delta(c,f);
        end
    end
    T = table(channel, label, condition, AUC, p, cliffs);
end


function plot_auc(auc, labels, faults, outDir)
    surface = [252 252 251]/255;
    ink     = [11 11 11]/255;
    muted   = [137 135 129]/255;

    fig = figure('Visible','off','Color',surface,'Position',[100 100 900 460]);
    ax  = axes(fig); %#ok<LAXES>
    imagesc(ax, auc, [0.5 1.0]);

    % One-hue sequential ramp: this is magnitude, not identity.
    blues = [205 226 251; 158 197 244; 109 167 236; 57 135 229; ...
             42 120 214; 28 92 171; 16 66 129]/255;
    colormap(ax, interp1(linspace(0,1,size(blues,1)), blues, linspace(0,1,256)));

    cb = colorbar(ax);
    cb.Label.String = 'AUC against healthy';
    cb.Color = muted;

    set(ax, 'XTick', 1:numel(faults), 'XTickLabel', ...
        strrep(cellstr(faults), '_', ' '), ...
        'YTick', 1:numel(labels), 'YTickLabel', cellstr(labels), ...
        'XColor', muted, 'YColor', muted, 'Color', surface, 'TickLength',[0 0]);
    title(ax, 'Single-channel detection power by fault', 'Color', ink, ...
          'FontSize', 13, 'FontWeight', 'normal');
    subtitle(ax, ['Direction-free AUC, so 0.5 is the floor as well as chance. ' ...
                  'n/a means too few runs to score.'], ...
             'Color', muted, 'FontSize', 10);

    for c = 1:size(auc,1)
        for f = 1:size(auc,2)
            if isnan(auc(c,f))
                text(ax, f, c, 'n/a', 'HorizontalAlignment','center', ...
                     'Color', muted, 'FontSize', 10);
                continue
            end
            if auc(c,f) > 0.82, txt = surface; else, txt = ink; end
            text(ax, f, c, sprintf('%.2f', auc(c,f)), ...
                 'HorizontalAlignment','center', 'Color', txt, 'FontSize', 10);
        end
    end

    exportgraphics(fig, fullfile(outDir, 'channel_separability.png'), ...
                   'Resolution', 160, 'BackgroundColor', surface);
    close(fig);
end
