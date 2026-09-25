import {DateRangePicker, DateRangePickerProps, FormField, Select, SpaceBetween} from '@cloudscape-design/components';
import {ReportingPeriod, ReportingSummaryRequest} from '../../client/reporting-model';

export const REPORTING_PERIODS = [
    {value: 'this_month', label: 'This month'},
    {value: 'last_month', label: 'Last month'},
    {value: 'last_30_days', label: 'Last 30 days'},
    {value: 'custom', label: 'Custom'}
];

export function clusterDate(timezone: string, date = new Date()): string {
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(date);
    return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type)!.value).join('-');
}

export function validateReportingPeriod(period: ReportingSummaryRequest, timezone?: string): string | undefined {
    if (period.period !== 'custom') return;
    const start = period.start_date ?? '';
    const end = period.end_date ?? '';
    const valid = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
    if (!valid(start) || !valid(end)) return 'Choose valid start and end dates.';
    if (start > end) return 'End date must be on or after start date.';
    if ((Date.parse(end) - Date.parse(start)) / 86400000 + 1 > 366) return 'Choose at most 366 days, including the end date.';
    if (timezone && end > clusterDate(timezone)) return 'Future dates are not available in the cluster timezone.';
}

export default function ReportingPeriodPicker({value, timezone, onChange}: {
    value: ReportingSummaryRequest;
    timezone?: string;
    onChange: (value: ReportingSummaryRequest) => void;
}) {
    const range: DateRangePickerProps.Value | null = value.start_date && value.end_date ? {type: 'absolute', startDate: value.start_date, endDate: value.end_date} : null;
    return <SpaceBetween size="s">
        <FormField label="Reporting period" description={timezone ? `Calendar dates in ${timezone}. Custom end date is inclusive; up to 366 days. Historical coverage may be incomplete.` : 'The server determines calendar dates and available history.'}>
            <Select selectedOption={REPORTING_PERIODS.find(option => option.value === value.period)!} options={REPORTING_PERIODS}
                onChange={({detail}) => onChange({period: detail.selectedOption.value as ReportingPeriod})}/>
        </FormField>
        {value.period === 'custom' && <FormField label="Custom dates" errorText={validateReportingPeriod(value, timezone)}>
            <DateRangePicker dateOnly rangeSelectorMode="absolute-only" relativeOptions={[]} value={range} ariaLabel="Custom reporting dates"
                placeholder="Start date — end date" showClearButton={false}
                isDateEnabled={date => !timezone || `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}` <= clusterDate(timezone)}
                dateDisabledReason={() => 'Future dates are not available.'}
                isValidRange={range => {
                    const error = validateReportingPeriod({period: 'custom', start_date: range?.type === 'absolute' ? range.startDate : '', end_date: range?.type === 'absolute' ? range.endDate : ''}, timezone);
                    return error ? {valid: false, errorMessage: error} : {valid: true};
                }}
                onChange={({detail}) => {
                    if (detail.value?.type === 'absolute') onChange({period: 'custom', start_date: detail.value.startDate, end_date: detail.value.endDate});
                }}
                i18nStrings={{applyButtonLabel: 'Apply', cancelButtonLabel: 'Cancel', startDateLabel: 'Start date', endDateLabel: 'End date', nextMonthAriaLabel: 'Next month', previousMonthAriaLabel: 'Previous month', todayAriaLabel: 'Today'}}/>
        </FormField>}
    </SpaceBetween>;
}
