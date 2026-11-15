FUNCTION z_bank_post_clear
  IMPORTING
    VALUE(iv_bukrs) TYPE bukrs
    VALUE(iv_kunnr) TYPE kunnr
    VALUE(iv_bldat) TYPE bldat
    VALUE(iv_budat) TYPE budat
    VALUE(iv_valut) TYPE valut OPTIONAL
    VALUE(iv_blart) TYPE blart DEFAULT 'DZ'
    VALUE(iv_monat) TYPE monat OPTIONAL
    VALUE(iv_waers) TYPE waers
    VALUE(iv_hkont) TYPE hkont
    VALUE(iv_wrbtr) TYPE wrbtr
    VALUE(iv_match_by) TYPE fieldname DEFAULT 'XBLNR'
    VALUE(iv_invoice_ref) TYPE char20
    VALUE(iv_utr) TYPE xblnr1 OPTIONAL
    VALUE(iv_sgtxt) TYPE sgtxt OPTIONAL
  EXPORTING
    VALUE(ev_status) TYPE char1
    VALUE(ev_belnr) TYPE belnr_d
    VALUE(ev_gjahr) TYPE gjahr
    VALUE(ev_augbl) TYPE augbl
    VALUE(ev_augdt) TYPE augdt
    VALUE(ev_message) TYPE bapi_msg.
*----------------------------------------------------------------------
* Bank receipt posting WITH clearing of the customer open item
* (replacement for the F-30 BDC in ZFI_BANK_PROGRAM).
* RFC-enabled; called by DataTwin via /sap/bc/soap/rfc.
* EV_STATUS: S = posted + cleared, W = posted, clearing not confirmed,
*            E = error, nothing posted
*----------------------------------------------------------------------

  DATA: lt_ftpost  TYPE STANDARD TABLE OF ftpost,
        lt_ftclear TYPE STANDARD TABLE OF ftclear,
        lt_fttax   TYPE STANDARD TABLE OF fttax,
        lt_blntab  TYPE STANDARD TABLE OF blntab,
        ls_ftpost  TYPE ftpost,
        ls_ftclear TYPE ftclear,
        ls_blntab  TYPE blntab,
        lv_kunnr   TYPE kunnr,
        lv_ref     TYPE char20,
        lv_belnr   TYPE belnr_d,
        lv_count   TYPE i,
        lv_date    TYPE char10,
        lv_amount  TYPE char20,
        lv_xblnr   TYPE xblnr1,
        lv_msgid   TYPE sy-msgid,
        lv_msgno   TYPE sy-msgno,
        lv_msgty   TYPE sy-msgty,
        lv_msgv1   TYPE sy-msgv1,
        lv_msgv2   TYPE sy-msgv2,
        lv_msgv3   TYPE sy-msgv3,
        lv_msgv4   TYPE sy-msgv4,
        lv_subrc   TYPE sy-subrc.

  CLEAR: ev_status, ev_belnr, ev_gjahr, ev_augbl, ev_augdt, ev_message.

  CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'
    EXPORTING input  = iv_kunnr
    IMPORTING output = lv_kunnr.

  lv_ref = iv_invoice_ref.
  IF iv_match_by = 'BELNR'.
    CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'
      EXPORTING input  = iv_invoice_ref
      IMPORTING output = lv_belnr.          " 10-char document number
    lv_ref = lv_belnr.
  ENDIF.

*--------------------------------------------------------------------*
* 1. Duplicate check: same UTR already posted in this company code
*--------------------------------------------------------------------*
  IF iv_utr IS NOT INITIAL.
    SELECT SINGLE belnr FROM bkpf INTO @DATA(lv_dup)
      WHERE bukrs = @iv_bukrs
        AND blart = @iv_blart
        AND xblnr = @iv_utr
        AND stblg = @space.
    IF sy-subrc = 0.
      ev_status  = 'E'.
      ev_message = |UTR { iv_utr } already posted in document { lv_dup }|.
      RETURN.
    ENDIF.
  ENDIF.

*--------------------------------------------------------------------*
* 2. Open item must exist
*--------------------------------------------------------------------*
  CASE iv_match_by.
    WHEN 'XBLNR'.
      SELECT COUNT(*) FROM bsid_view INTO @lv_count
        WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr AND xblnr = @lv_ref.
    WHEN 'ZUONR'.
      SELECT COUNT(*) FROM bsid_view INTO @lv_count
        WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr AND zuonr = @lv_ref.
    WHEN 'BELNR'.
      SELECT COUNT(*) FROM bsid_view INTO @lv_count
        WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr AND belnr = @lv_belnr.
    WHEN OTHERS.
      ev_status  = 'E'.
      ev_message = |MatchBy { iv_match_by } not supported (XBLNR/ZUONR/BELNR)|.
      RETURN.
  ENDCASE.

  IF lv_count = 0.
    ev_status  = 'E'.
    ev_message = |No open item for customer { lv_kunnr } with { iv_match_by } = { lv_ref }|.
    RETURN.
  ENDIF.

*--------------------------------------------------------------------*
* 3. Build posting data (values in user format, like batch input)
*--------------------------------------------------------------------*
  DEFINE add_post.
    CLEAR ls_ftpost.
    ls_ftpost-stype = &1.
    ls_ftpost-count = &2.
    ls_ftpost-fnam  = &3.
    ls_ftpost-fval  = &4.
    APPEND ls_ftpost TO lt_ftpost.
  END-OF-DEFINITION.

  lv_xblnr = COND #( WHEN iv_utr IS NOT INITIAL THEN iv_utr ELSE iv_invoice_ref ).

  " Header (K)
  WRITE iv_bldat TO lv_date.
  add_post 'K' 1 'BKPF-BLDAT' lv_date.
  WRITE iv_budat TO lv_date.
  add_post 'K' 1 'BKPF-BUDAT' lv_date.
  add_post 'K' 1 'BKPF-BLART' iv_blart.
  add_post 'K' 1 'BKPF-BUKRS' iv_bukrs.
  add_post 'K' 1 'BKPF-WAERS' iv_waers.
  add_post 'K' 1 'BKPF-XBLNR' lv_xblnr.
  IF iv_monat IS NOT INITIAL.
    add_post 'K' 1 'BKPF-MONAT' iv_monat.
  ENDIF.

  " Bank line (P) : posting key 40 = debit G/L
  WRITE iv_wrbtr TO lv_amount CURRENCY iv_waers LEFT-JUSTIFIED.
  add_post 'P' 1 'RF05A-NEWBS' '40'.
  add_post 'P' 1 'BSEG-HKONT'  iv_hkont.
  add_post 'P' 1 'BSEG-WRBTR'  lv_amount.
  add_post 'P' 1 'BSEG-ZUONR'  iv_invoice_ref.
  IF iv_sgtxt IS NOT INITIAL.
    add_post 'P' 1 'BSEG-SGTXT' iv_sgtxt.
  ENDIF.
  IF iv_valut IS NOT INITIAL.
    WRITE iv_valut TO lv_date.
    add_post 'P' 1 'BSEG-VALUT' lv_date.
  ENDIF.

  " Open item selection (customer side is generated by the clearing)
  ls_ftclear-agkoa  = 'D'.
  ls_ftclear-agkon  = lv_kunnr.
  ls_ftclear-agbuk  = iv_bukrs.
  ls_ftclear-xnops  = 'X'.
  ls_ftclear-selfd  = iv_match_by.
  ls_ftclear-selvon = lv_ref.
  APPEND ls_ftclear TO lt_ftclear.

*--------------------------------------------------------------------*
* 4. Post with clearing (incoming payment, FB05 logic, call transaction)
*--------------------------------------------------------------------*
  CALL FUNCTION 'POSTING_INTERFACE_START'
    EXPORTING
      i_function         = 'C'
      i_mode             = 'N'
      i_update           = 'S'
      i_user             = sy-uname
    EXCEPTIONS
      client_incorrect   = 1
      function_invalid   = 2
      group_name_missing = 3
      mode_invalid       = 4
      update_invalid     = 5
      OTHERS             = 6.
  IF sy-subrc <> 0.
    ev_status  = 'E'.
    ev_message = |POSTING_INTERFACE_START failed (rc { sy-subrc })|.
    RETURN.
  ENDIF.

  CALL FUNCTION 'POSTING_INTERFACE_CLEARING'
    EXPORTING
      i_auglv                    = 'EINGZAHL'
      i_tcode                    = 'FB05'
      i_sgfunct                  = 'C'
    IMPORTING
      e_msgid                    = lv_msgid
      e_msgno                    = lv_msgno
      e_msgty                    = lv_msgty
      e_msgv1                    = lv_msgv1
      e_msgv2                    = lv_msgv2
      e_msgv3                    = lv_msgv3
      e_msgv4                    = lv_msgv4
      e_subrc                    = lv_subrc
    TABLES
      t_blntab                   = lt_blntab
      t_ftclear                  = lt_ftclear
      t_ftpost                   = lt_ftpost
      t_fttax                    = lt_fttax
    EXCEPTIONS
      clearing_procedure_invalid = 1
      clearing_procedure_missing = 2
      table_t041a_empty          = 3
      transaction_code_invalid   = 4
      amount_format_error        = 5
      too_many_line_items        = 6
      company_code_invalid       = 7
      screen_not_found           = 8
      no_authorization           = 9
      OTHERS                     = 10.
  DATA(lv_exc) = sy-subrc.

  CALL FUNCTION 'POSTING_INTERFACE_END'
    EXCEPTIONS
      session_not_processable = 1
      OTHERS                  = 2.

  IF lv_msgid IS NOT INITIAL.
    MESSAGE ID lv_msgid TYPE 'S' NUMBER lv_msgno
      WITH lv_msgv1 lv_msgv2 lv_msgv3 lv_msgv4 INTO ev_message.
  ENDIF.

  READ TABLE lt_blntab INTO ls_blntab INDEX 1.
  IF lv_exc <> 0 OR lv_subrc <> 0 OR ls_blntab-belnr IS INITIAL.
    ev_status = 'E'.
    IF ev_message IS INITIAL.
      ev_message = |Posting failed (exception { lv_exc }, rc { lv_subrc })|.
    ENDIF.
    RETURN.
  ENDIF.

  ev_belnr = ls_blntab-belnr.
  ev_gjahr = ls_blntab-gjahr.

*--------------------------------------------------------------------*
* 5. Confirm the invoice is now cleared by this document
*--------------------------------------------------------------------*
  SELECT SINGLE augbl, augdt FROM bsad_view
    INTO (@ev_augbl, @ev_augdt)
    WHERE bukrs = @iv_bukrs
      AND kunnr = @lv_kunnr
      AND augbl = @ev_belnr.
  IF sy-subrc = 0.
    ev_status  = 'S'.
    ev_message = |Document { ev_belnr } posted and cleared { iv_match_by } { lv_ref }|.
  ELSE.
    ev_status  = 'W'.
    ev_message = |Document { ev_belnr } posted, but no cleared item found in BSAD - check FBL5N|.
  ENDIF.

ENDFUNCTION.
