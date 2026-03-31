const housingData = {
    sfh: {
        counties: ['Hawaiʻi (State)', 'Honolulu', 'Maui', 'Hawaiʻi Island', 'Kauaʻi'],
        medianPrice: [950000, 1092400, 1195000, 433750, 1080000],
        medianIncome: [98317, 104264, 95067, 77215, 93612],
        monthlyPayment: [4610.47, 5301.56, 5799.49, 2105.04, 5241.38],
        paymentToIncome: [56.27, 61.02, 73.21, 32.71, 67.19],
        affordabilityIndex: [53, 49, 41, 92, 45],
        downPayment: [190000, 218480, 239000, 86750, 216000]
    },
    condo: {
        counties: ['Hawaiʻi (State)', 'Honolulu', 'Maui', 'Hawaiʻi Island', 'Kauaʻi'],
        medianPrice: [600000, 560000, 912500, 550000, 950000],
        medianIncome: [98317, 104264, 95067, 77215, 93612],
        monthlyPayment: [2911.88, 2717.75, 4428.48, 2669.22, 4610.47],
        paymentToIncome: [35.54, 31.28, 55.90, 41.48, 59.10],
        affordabilityIndex: [84, 96, 54, 72, 51],
        downPayment: [120000, 112000, 182500, 110000, 190000]
    }
};

let currentType = 'sfh';
let currentGeography = 0;

function formatCurrency(value) {
    return new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: 0,
        maximumFractionDigits: 0
    }).format(value);
}

function formatPercent(value) {
    return value.toFixed(2) + '%';
}

function getAffordabilityClass(index) {
    if (index >= 80) return 'index-high';
    if (index >= 60) return 'index-medium';
    return 'index-low';
}

function getAffordabilityLabel(index) {
    if (index >= 80) return 'Good';
    if (index >= 60) return 'Moderate';
    return 'Low';
}

function renderMetrics() {
    const data = housingData[currentType];
    const metricsGrid = document.getElementById('metricsGrid');
    
    const metrics = [
        {
            label: 'Median Price',
            value: formatCurrency(data.medianPrice[currentGeography]),
            change: null
        },
        {
            label: 'Median Income',
            value: formatCurrency(data.medianIncome[currentGeography]),
            change: null
        },
        {
            label: 'Monthly Payment',
            value: formatCurrency(data.monthlyPayment[currentGeography]),
            change: null
        },
        {
            label: 'Affordability Index',
            value: data.affordabilityIndex[currentGeography],
            change: null
        }
    ];
    
    const icons = {
        'Median Price': '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#4a6b52" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>',
        'Median Income': '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#4a6b52" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>',
        'Monthly Payment': '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#4a6b52" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="1" y="4" width="22" height="16" rx="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>',
        'Affordability Index': '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#4a6b52" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>'
    };
    
    metricsGrid.innerHTML = metrics.map(metric => `
        <div class="figure-cell">
            <div class="figure-cell__accent"></div>
            <div class="figure-cell__icon">${icons[metric.label] || ''}</div>
            <div class="figure-cell__label">${metric.label}</div>
            <div class="figure-cell__value">${metric.value}</div>
        </div>
    `).join('');
}

function renderPriceChart() {
    const data = housingData[currentType];
    const container = document.getElementById('priceChart');
    
    const maxPrice = Math.max(...data.medianPrice);
    const statePercent = (data.medianPrice[0] / maxPrice * 100);
    
    const html = `
        <div class="bar-chart">
            ${data.counties.map((county, index) => `
                <div class="bar-item">
                    <div class="bar-label">${county}</div>
                    <div class="bar-container">
                        <div class="bar-fill" style="width: ${(data.medianPrice[index] / maxPrice * 100)}%; animation-delay: ${index * 0.15}s">
                            <span class="bar-value">${formatCurrency(data.medianPrice[index])}</span>
                        </div>
                        ${index > 0 ? `<div class="reference-line" style="left: ${statePercent}%"><div class="reference-line-label">${index === 1 ? 'State' : ''}</div></div>` : ''}
                    </div>
                </div>
            `).join('')}
        </div>
    `;
    
    container.innerHTML = html;
}

function renderAffordabilityChart() {
    const data = housingData[currentType];
    const container = document.getElementById('affordabilityChart');
    
    const stateIndex = data.affordabilityIndex[0];
    
    const html = `
        <div class="bar-chart">
            ${data.counties.map((county, index) => {
                const affordIndex = data.affordabilityIndex[index];
                return `
                    <div class="bar-item">
                        <div class="bar-label">${county}</div>
                        <div class="bar-container">
                            <div class="bar-fill" style="width: ${affordIndex}%; background: ${
                                affordIndex >= 80 ? 'linear-gradient(90deg, #10B981 0%, #34D399 100%)' :
                                affordIndex >= 60 ? 'linear-gradient(90deg, #F59E0B 0%, #FBBF24 100%)' :
                                'linear-gradient(90deg, #EF4444 0%, #F87171 100%)'
                            }; animation-delay: ${index * 0.15}s">
                                <span class="bar-value">${affordIndex}</span>
                            </div>
                            ${index > 0 ? `<div class="reference-line" style="left: ${stateIndex}%"><div class="reference-line-label">${index === 1 ? 'State' : ''}</div></div>` : ''}
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
    
    container.innerHTML = html;
}

function renderPaymentIncomeChart() {
    const data = housingData[currentType];
    const container = document.getElementById('paymentIncomeChart');
    
    const html = `
        <div class="treemap-container">
            ${data.counties.map((county, index) => {
                const ratio = data.paymentToIncome[index];
                let category = 'good';
                if (ratio >= 50) category = 'low';
                else if (ratio >= 30) category = 'moderate';
                
                // Calculate height of payment box based on percentage (max 80% of container)
                const paymentHeight = Math.min(ratio * 1.2, 120); // Max 120px height
                
                return `
                    <div class="treemap-wrapper">
                        <div class="treemap-county-header">${county}</div>
                        <div class="treemap-box">
                            <div class="treemap-payment-box ${category}" style="height: ${paymentHeight}px">
                                <div class="treemap-payment-value">${formatPercent(ratio)}</div>
                                <div class="treemap-payment-label">Payment</div>
                            </div>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
    
    container.innerHTML = html;
}

function renderDataTable() {
    const data = housingData[currentType];
    const container = document.getElementById('dataTable');
    
    const propertyType = currentType === 'sfh' ? 'Single Family Home' : 'Condo';
    
    const html = `
        <table>
            <thead>
                <tr>
                    <th>County</th>
                    <th>Median Price</th>
                    <th>Median Income</th>
                    <th>Monthly Payment</th>
                    <th>Payment/Income %</th>
                    <th>Affordability Index</th>
                    <th>Down Payment</th>
                </tr>
            </thead>
            <tbody>
                ${data.counties.map((county, index) => {
                    const isStateRow = index === 0; // First row is always Hawaii (State)
                    return `
                    <tr class="${isStateRow ? 'state-row' : ''}">
                        <td><strong>${county}</strong></td>
                        <td>${formatCurrency(data.medianPrice[index])}</td>
                        <td>${formatCurrency(data.medianIncome[index])}</td>
                        <td>${formatCurrency(data.monthlyPayment[index])}</td>
                        <td>${formatPercent(data.paymentToIncome[index])}</td>
                        <td>
                            <span class="index-indicator ${getAffordabilityClass(data.affordabilityIndex[index])}">
                                ${data.affordabilityIndex[index]} - ${getAffordabilityLabel(data.affordabilityIndex[index])}
                            </span>
                        </td>
                        <td>${formatCurrency(data.downPayment[index])}</td>
                    </tr>
                `;
                }).join('')}
            </tbody>
        </table>
    `;
    
    container.innerHTML = html;
}

function renderAll() {
    renderMetrics();
    renderPriceChart();
    renderAffordabilityChart();
    renderPaymentIncomeChart();
    renderDataTable();
}

document.addEventListener('DOMContentLoaded', () => {
    renderAll();
    
    const tabs = document.querySelectorAll('.toggle-btn');
    const hoaDisclaimer = document.getElementById('hoaDisclaimer');
    
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            
            currentType = tab.dataset.type;
            
            // Show/hide HOA disclaimer based on property type
            if (currentType === 'condo') {
                hoaDisclaimer.style.display = 'flex';
            } else {
                hoaDisclaimer.style.display = 'none';
            }
            
            renderAll();
        });
    });
    
    const geographySelect = document.getElementById('geographySelect');
    if (geographySelect) {
        geographySelect.addEventListener('change', (e) => {
            console.log('Dropdown changed to:', e.target.value);
            currentGeography = parseInt(e.target.value);
            console.log('Current geography set to:', currentGeography);
            renderMetrics();
        });
    } else {
        console.error('Geography select not found');
    }
});
