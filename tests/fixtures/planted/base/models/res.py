from odoo import fields, models


class ResPartner(models.Model):
    _name = 'res.partner'
    _description = 'Contact'

    name = fields.Char()
    email = fields.Char()
    country_id = fields.Many2one('res.country')
    company_id = fields.Many2one('res.company')


class ResCountry(models.Model):
    _name = 'res.country'
    _description = 'Country'

    name = fields.Char()
    code = fields.Char()


class ResCompany(models.Model):
    _name = 'res.company'
    _description = 'Company'

    name = fields.Char()
    currency_id = fields.Many2one('res.currency')


class ResCurrency(models.Model):
    _name = 'res.currency'
    _description = 'Currency'

    name = fields.Char()


class ResUsers(models.Model):
    _name = 'res.users'
    _description = 'User'
    _inherits = {'res.partner': 'partner_id'}

    partner_id = fields.Many2one('res.partner', required=True)
    login = fields.Char()


class ResGroups(models.Model):
    _name = 'res.groups'
    _description = 'Group'

    name = fields.Char()
