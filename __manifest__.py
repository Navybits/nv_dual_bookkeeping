{
    'name': 'NV Dual Bookkeeping',
    'version': '19.0.1.0.1',
    'summary': 'Official / Non-Official dual-sequence bookkeeping for Lebanese companies',
    'author': 'Navybits',
    'license': 'OPL-1',
    'category': 'Accounting/Accounting',
    'depends': ['accountant','nv_double_currencies'],
    'post_init_hook': 'post_init_hook',

    'data': [
        # Security — must load before views and models reference groups
        'security/nv_dual_bookkeeping_security.xml',
        'security/ir.model.access.csv',

        # Default config parameters
        'data/nv_dual_bookkeeping_data.xml',

        # Wizard views
        'wizard/views/nv_journal_link_wizard_views.xml',
        'wizard/views/account_payment_register_views.xml',

        # Model views
        'views/res_partner_views.xml',
        'views/account_journal_views.xml',
        'views/account_move_views.xml',
        'views/account_payment_views.xml',
        'views/res_company_views.xml',
        'views/nv_sync_log_views.xml',
        'views/res_config_settings_views.xml',
        'views/nv_account_account.xml',

        # Report overrides (invoice layout + external layout patches)
        'views/report_invoice.xml',
        'views/report_layouts.xml',

        # Menus last (depend on actions defined in view files)
        'views/dual_bookkeeping_menus.xml',

    ],

    'installable': True,
    'application': False,
    'auto_install': False,
}
