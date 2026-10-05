from odoo import api, fields, models


class LibraryBook(models.Model):
    _inherit = 'library.book'

    reviewer_ids = fields.One2many('library.author', 'reviewer_id')
    blurb = fields.Char(related='author_id.missing_field')
    score = fields.Integer(compute='_compute_score')
    rank = fields.Integer(compute='_compute_rank', store=True)
    shelf_id = fields.Many2one('library.shelf')
    origin = fields.Char(compute='_compute_origin')

    def _compute_rank(self):
        for rec in self:
            rec.rank = 0

    @api.depends('author_id.birthplace')
    def _compute_origin(self):
        for rec in self:
            rec.origin = False


class LibraryAuthor(models.Model):
    _inherit = 'library.author'

    fee = fields.Monetary()


class LibraryLoan(models.Model):
    _name = 'library.loan'
    _description = 'Loan'
    _order = 'loan_date desc'

    book_id = fields.Many2one('library.book')
